// "Tocando agora": o que o Windows mostra no controle de mídia (Spotify, YouTube no navegador, players…).

using System.Security.Cryptography;
using Windows.Media.Control;

sealed class MediaWatcher
{
    GlobalSystemMediaTransportControlsSessionManager? mgr;
    string coverPath = "";
    string coverKey = "";

    public async Task<object?> Poll()
    {
        mgr ??= await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
        var s = mgr.GetCurrentSession();
        if (s == null) return null;
        var props = await s.TryGetMediaPropertiesAsync();
        var info = s.GetPlaybackInfo();
        var tl = s.GetTimelineProperties();
        string title = props?.Title ?? "";
        string artist = props?.Artist ?? "";
        if (string.IsNullOrEmpty(artist)) artist = props?.AlbumArtist ?? "";
        string album = props?.AlbumTitle ?? "";
        string status = info?.PlaybackStatus.ToString() ?? "Unknown";

        string key = title + "\u0001" + artist;
        if (key != coverKey && props?.Thumbnail != null)
        {
            coverKey = key;
            coverPath = "";
            try
            {
                using var ras = await props.Thumbnail.OpenReadAsync();
                using var st = ras.AsStreamForRead();
                using var ms = new MemoryStream();
                await st.CopyToAsync(ms);
                var bytes = ms.ToArray();
                if (bytes.Length > 0)
                {
                    var hash = Convert.ToHexString(SHA1.HashData(bytes))[..16];
                    var path = Path.Combine(Path.GetTempPath(), $"digoolcd_capa_{hash}.img");
                    if (!File.Exists(path)) File.WriteAllBytes(path, bytes);
                    coverPath = path;
                }
            }
            catch { }
        }
        else if (key != coverKey)
        {
            coverKey = key;
            coverPath = "";
        }

        double pos = tl.Position.TotalSeconds;
        double dur = (tl.EndTime - tl.StartTime).TotalSeconds;
        if (status == "Playing" && tl.LastUpdatedTime.Year > 2000)
            pos += Math.Max(0, (DateTimeOffset.Now - tl.LastUpdatedTime).TotalSeconds);
        return new
        {
            title, artist, album, status,
            app = s.SourceAppUserModelId ?? "",
            position = Math.Round(Math.Min(pos, dur > 0 ? dur : pos), 1),
            duration = Math.Round(Math.Max(0, dur), 1),
            cover = coverPath,
        };
    }
}
