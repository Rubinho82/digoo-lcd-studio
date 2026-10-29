// Som que sai do PC (WASAPI loopback, sem microfone) → espectro em bandas logarítmicas.

using NAudio.Wave;

sealed class AudioMeter : IDisposable
{
    const int N = 2048;                    // janela da FFT (~43 ms a 48 kHz)
    readonly float[] ring = new float[N * 2];
    int pos;
    long lastData;                         // ms do último pacote de áudio
    readonly object lk = new();
    WasapiLoopbackCapture? cap;
    int rate = 48000;
    double peak = 1e-4;                    // ganho automático (pico recente)
    volatile bool disposed;

    public bool Active => Environment.TickCount64 - Interlocked.Read(ref lastData) < 250;

    public void Start()
    {
        try
        {
            var c = new WasapiLoopbackCapture();
            cap = c;
            rate = c.WaveFormat.SampleRate;
            int ch = c.WaveFormat.Channels;
            int bps = c.WaveFormat.BitsPerSample / 8;
            bool isFloat = c.WaveFormat.Encoding == WaveFormatEncoding.IeeeFloat
                           || (c.WaveFormat.Encoding == WaveFormatEncoding.Extensible && bps == 4);
            c.DataAvailable += (_, e) =>
            {
                int frames = e.BytesRecorded / (bps * ch);
                lock (lk)
                {
                    for (int i = 0; i < frames; i++)
                    {
                        float s = 0;
                        for (int k = 0; k < ch; k++)
                        {
                            int off = (i * ch + k) * bps;
                            s += isFloat ? BitConverter.ToSingle(e.Buffer, off) : BitConverter.ToInt16(e.Buffer, off) / 32768f;
                        }
                        ring[pos] = s / ch;
                        pos = (pos + 1) % ring.Length;
                    }
                }
                if (frames > 0) Interlocked.Exchange(ref lastData, Environment.TickCount64);
            };
            c.RecordingStopped += (_, _) =>
            {
                // trocou fone/caixa de som etc.: reabre o dispositivo padrão
                if (disposed) return;
                Task.Delay(2000).ContinueWith(_ => { try { c.Dispose(); } catch { } Start(); });
            };
            c.StartRecording();
        }
        catch
        {
            if (!disposed) Task.Delay(5000).ContinueWith(_ => Start());
        }
    }

    /// <summary>Bandas 0..100 (logarítmicas entre 40 Hz e 16 kHz) e nível geral 0..100.</summary>
    public (int[] bands, int level) Read(int nBands)
    {
        var re = new double[N];
        var im = new double[N];
        lock (lk)
        {
            int start = (pos - N + ring.Length) % ring.Length;
            for (int i = 0; i < N; i++)
            {
                double w = 0.5 - 0.5 * Math.Cos(2 * Math.PI * i / (N - 1));  // janela de Hann
                re[i] = ring[(start + i) % ring.Length] * w;
            }
        }
        double rms = 0;
        for (int i = 0; i < N; i++) rms += re[i] * re[i];
        rms = Math.Sqrt(rms / N);
        Fft(re, im);

        var mags = new double[nBands];
        double fMin = 40, fMax = Math.Min(16000, rate / 2.0);
        for (int b = 0; b < nBands; b++)
        {
            double f0 = fMin * Math.Pow(fMax / fMin, (double)b / nBands);
            double f1 = fMin * Math.Pow(fMax / fMin, (double)(b + 1) / nBands);
            int k0 = Math.Max(1, (int)(f0 * N / rate));
            int k1 = Math.Max(k0 + 1, (int)(f1 * N / rate));
            double m = 0;
            for (int k = k0; k < k1 && k < N / 2; k++)
                m = Math.Max(m, Math.Sqrt(re[k] * re[k] + im[k] * im[k]));
            mags[b] = m * (1 + b * 0.06);  // compensa os agudos, que naturalmente têm menos energia
        }
        double top = 0;
        foreach (var m in mags) top = Math.Max(top, m);
        peak = Math.Max(top, peak * 0.995);   // ganho automático: volume baixo também mexe as barras
        var outB = new int[nBands];
        for (int b = 0; b < nBands; b++)
        {
            double db = 20 * Math.Log10(mags[b] / (peak + 1e-9) + 1e-9);   // 0 dB = pico recente
            outB[b] = (int)Math.Clamp((db + 45) / 45 * 100, 0, 100);
        }
        int level = (int)Math.Clamp(rms * 400, 0, 100);
        return (outB, level);
    }

    static void Fft(double[] re, double[] im)
    {
        int n = re.Length;
        for (int i = 1, j = 0; i < n; i++)
        {
            int bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) { (re[i], re[j]) = (re[j], re[i]); (im[i], im[j]) = (im[j], im[i]); }
        }
        for (int len = 2; len <= n; len <<= 1)
        {
            double ang = -2 * Math.PI / len;
            double wr = Math.Cos(ang), wi = Math.Sin(ang);
            for (int i = 0; i < n; i += len)
            {
                double cr = 1, ci = 0;
                for (int k = 0; k < len / 2; k++)
                {
                    int a = i + k, b = a + len / 2;
                    double tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
                    re[b] = re[a] - tr; im[b] = im[a] - ti;
                    re[a] += tr; im[a] += ti;
                    double ncr = cr * wr - ci * wi;
                    ci = cr * wi + ci * wr;
                    cr = ncr;
                }
            }
        }
    }

    public void Dispose()
    {
        disposed = true;
        try { cap?.StopRecording(); cap?.Dispose(); } catch { }
    }
}
