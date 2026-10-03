// ACT host hooks handle playback. These Mono System.Speech stubs remain silent.
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
