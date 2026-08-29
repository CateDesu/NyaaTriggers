// Minimal WMPLib (Windows Media Player COM) stub for headless Mono/Linux hosting.
// Mono cannot generate the COM interop assembly the csproj's <COMReference> expects,
// and WMP does not exist on Linux. The engine self-degrades (WMPUnavailable=true) if
// construction fails, and with cfg.SoundMethod=ACT all sound routes through our
// SoundPlaybackHook, so a no-op player is correct. This stub exists only to (a) compile
// the 8 files that reference WMPLib and (b) let `new WindowsMediaPlayer()` succeed.
namespace WMPLib
{
    public enum WMPPlayState
    {
        wmppsUndefined = 0, wmppsStopped = 1, wmppsPaused = 2, wmppsPlaying = 3,
        wmppsScanForward = 4, wmppsScanReverse = 5, wmppsBuffering = 6, wmppsWaiting = 7,
        wmppsMediaEnded = 8, wmppsTransitioning = 9, wmppsReady = 10,
        wmppsReconnecting = 11, wmppsLast = 12
    }

    public delegate void _WMPOCXEvents_PlayStateChangeEventHandler(int NewState);
    public delegate void _WMPOCXEvents_MediaErrorEventHandler(object pMediaObject);

    public interface IWMPSettings { int volume { get; set; } }

    internal sealed class WmpSettings : IWMPSettings { public int volume { get; set; } }

    public class WindowsMediaPlayer
    {
        private readonly IWMPSettings _settings = new WmpSettings();
        public string URL { get; set; }
        public IWMPSettings settings { get { return _settings; } }
        public WMPPlayState playState { get { return WMPPlayState.wmppsStopped; } }
#pragma warning disable 67
        public event _WMPOCXEvents_PlayStateChangeEventHandler PlayStateChange;
        public event _WMPOCXEvents_MediaErrorEventHandler MediaError;
#pragma warning restore 67
        public void close() { }
    }
}
