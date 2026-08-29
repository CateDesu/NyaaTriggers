// Minimal System.Speech stub for headless Mono/Linux hosting of Triggernometry.
// The engine only constructs a SpeechSynthesizer and sets Volume/Rate/SpeakAsync;
// with cfg.TtsMethod=ACT all real TTS is routed through our TtsPlaybackHook, so a
// no-op synthesizer is correct. Exists purely so (a) the engine compiles under Mono
// where the real System.Speech is absent, and (b) `new SpeechSynthesizer()` at
// RealPlugin.cs:2287 does not throw (which would silently set isInitialized=false).
namespace System.Speech.Synthesis
{
    public class SpeechSynthesizer : System.IDisposable
    {
        public int Volume { get; set; }
        public int Rate { get; set; }
        public void SpeakAsync(string textToSpeak) { }
        public void Speak(string textToSpeak) { }
        public void Dispose() { }
    }
}
