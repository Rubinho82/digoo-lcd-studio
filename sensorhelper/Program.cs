// sensorhelper: lê os sensores via LibreHardwareMonitor e escreve
// uma linha JSON por ciclo no stdout. O app Python consome essas linhas.
//
// Uso: sensorhelper.exe [intervalo_ms] [--audio]     (padrão 1000)
//   --audio : também manda o espectro do som do PC (~30x/s) — {"audio":{"b":[...],"lv":n}}
// Sempre manda o "tocando agora" do Windows quando muda — {"media":{...}} ou {"media":null}
// Encerra sozinho quando o stdin fecha (processo pai morreu).
//
// Econômico: RAM vem do psutil no app (não liga o leitor de memória/SPD daqui), fonte e
// controladoras USB ficam desligadas, SSDs só são lidos a cada 10 ciclos, e a memória
// ociosa é devolvida ao Windows de tempos em tempos.

using System.Runtime;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;

int interval = args.Length > 0 && int.TryParse(args[0], out var ms) ? Math.Clamp(ms, 250, 10000) : 1000;

var computer = new Computer
{
    IsCpuEnabled = true,
    IsGpuEnabled = true,
    IsMemoryEnabled = false,
    IsMotherboardEnabled = true,
    IsControllerEnabled = false,
    IsStorageEnabled = true,
    IsNetworkEnabled = false,
    IsPsuEnabled = false,
    IsBatteryEnabled = false,
};

var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
var outLock = new object();
void Emit(string line)
{
    lock (outLock) stdout.WriteLine(line);
}
bool wantAudio = args.Contains("--audio");

try
{
    computer.Open();
}
catch (Exception ex)
{
    Emit(JsonSerializer.Serialize(new { error = "open: " + ex.Message }));
}

int gpuFanTarget = -1;   // -1 = automático
bool gpuFanManual = false;

// Watchdog: se o pai fechar o pipe, saímos.
var stop = new CancellationTokenSource();
_ = Task.Run(() =>
{
    // também recebe comandos do app: "gpufan 70" | "gpufan auto"
    try
    {
        var rd = new StreamReader(Console.OpenStandardInput());
        string? line;
        while ((line = rd.ReadLine()) != null)
        {
            var p = line.Trim().Split(' ');
            if (p.Length == 2 && p[0] == "gpufan")
                gpuFanTarget = p[1] == "auto" ? -1 : Math.Clamp(int.TryParse(p[1], out var v) ? v : 100, 30, 100);
        }
    }
    catch { }
    stop.Cancel();
});

// som do PC → espectro, ~30 vezes por segundo (só enquanto há som saindo)
AudioMeter? meter = null;
if (wantAudio)
{
    meter = new AudioMeter();
    meter.Start();
    _ = Task.Run(async () =>
    {
        bool wasActive = false;
        while (!stop.IsCancellationRequested)
        {
            try
            {
                bool active = meter.Active;
                if (active || wasActive)
                {
                    var (bands, level) = active ? meter.Read(48) : (new int[48], 0);
                    Emit(JsonSerializer.Serialize(new { audio = new { b = bands, lv = level } }));
                }
                wasActive = active;
            }
            catch (IOException) { stop.Cancel(); }
            catch { }
            await Task.Delay(33);
        }
    });
}

// "tocando agora" (Spotify, YouTube no navegador…): manda quando muda e a cada 5 s para sincronizar o tempo
_ = Task.Run(async () =>
{
    var watcher = new MediaWatcher();
    string last = "";
    long lastSent = 0;
    while (!stop.IsCancellationRequested)
    {
        try
        {
            var info = await watcher.Poll();
            string json = JsonSerializer.Serialize(new { media = info });
            string key = info == null ? "null" : System.Text.RegularExpressions.Regex.Replace(json, "\"position\":[0-9.]+", "");
            if (key != last || Environment.TickCount64 - lastSent > 5000)
            {
                Emit(json);
                last = key;
                lastSent = Environment.TickCount64;
            }
        }
        catch (IOException) { stop.Cancel(); }
        catch { }
        await Task.Delay(1000);
    }
});

var visitor = new UpdateVisitor();
var sensors = new List<SensorRow>(320);
var jsonOpts = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
long cycle = 0;
while (!stop.IsCancellationRequested)
{
    sensors.Clear();
    visitor.IncludeStorage = cycle % 10 == 0;
    try
    {
        computer.Accept(visitor);
        foreach (var hw in computer.Hardware)
            Collect(hw, sensors);
    }
    catch (Exception ex)
    {
        Emit(JsonSerializer.Serialize(new { error = "update: " + ex.Message }));
    }

    try
    {
        Emit(JsonSerializer.Serialize(new Packet(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), sensors), jsonOpts));
    }
    catch (IOException)
    {
        break; // pai foi embora
    }

    try { ApplyGpuFan(); } catch { }

    if (cycle % 60 == 5)
        Trim();
    cycle++;

    try { Task.Delay(interval, stop.Token).Wait(); } catch { }
}

gpuFanTarget = -1;
try { ApplyGpuFan(); } catch { }   // ao sair, a placa volta para o automático
meter?.Dispose();
computer.Close();
return 0;

void ApplyGpuFan()
{
    int target = gpuFanTarget;
    if (target < 0 && !gpuFanManual) return;
    foreach (var hw in computer.Hardware)
    {
        if (hw.HardwareType is not (HardwareType.GpuNvidia or HardwareType.GpuAmd or HardwareType.GpuIntel)) continue;
        float temp = hw.Sensors.Where(s => s.SensorType == SensorType.Temperature && s.Name == "GPU Core")
                              .Select(s => s.Value ?? 0).DefaultIfEmpty(0).Max();
        foreach (var s in hw.Sensors)
        {
            if (s.SensorType != SensorType.Control || s.Control == null) continue;
            if (target < 0) s.Control.SetDefault();
            else s.Control.SetSoftware(temp > 83 ? 100 : target);   // proteção: GPU quente = 100%
        }
    }
    gpuFanManual = target >= 0;
}

static void Collect(IHardware hw, List<SensorRow> outList)
{
    foreach (var s in hw.Sensors)
    {
        if (s.Value is not float v || float.IsNaN(v) || float.IsInfinity(v)) continue;
        outList.Add(new SensorRow(s.Identifier.ToString(), hw.Name, hw.HardwareType.ToString(),
                                  s.SensorType.ToString(), s.Name, Math.Round(v, 2)));
    }
    foreach (var sub in hw.SubHardware)
        Collect(sub, outList);
}

static void Trim()
{
    // devolve ao Windows o que o .NET guardou "por via das dúvidas"
    GCSettings.LargeObjectHeapCompactionMode = GCLargeObjectHeapCompactionMode.CompactOnce;
    GC.Collect(2, GCCollectionMode.Forced, true, true);
    try { Native.EmptyWorkingSet(Native.GetCurrentProcess()); } catch { }
}

record SensorRow(string Id, string Hw, string HwType, string Type, string Name, double Value);
record Packet(long T, List<SensorRow> Sensors);

static class Native
{
    [DllImport("psapi.dll")] public static extern bool EmptyWorkingSet(IntPtr hProcess);
    [DllImport("kernel32.dll")] public static extern IntPtr GetCurrentProcess();
}

sealed class UpdateVisitor : IVisitor
{
    public bool IncludeStorage = true;

    public void VisitComputer(IComputer computer) => computer.Traverse(this);
    public void VisitHardware(IHardware hardware)
    {
        if (hardware.HardwareType == HardwareType.Storage && !IncludeStorage) return;
        hardware.Update();
        foreach (var sub in hardware.SubHardware) sub.Accept(this);
    }
    public void VisitSensor(ISensor sensor) { }
    public void VisitParameter(IParameter parameter) { }
}
