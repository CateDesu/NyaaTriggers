// Provide the System.Speech types required to compile and initialize Triggernometry
// under Mono. TtsMethod=ACT routes speech through the host hook, so these methods need
// no playback.
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
