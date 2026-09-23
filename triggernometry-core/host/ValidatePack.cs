using System;
using System.IO;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Serialization;
using Triggernometry;

static class ValidatePack
{
    static int Main()
    {
        try
        {
            Console.InputEncoding = new System.Text.UTF8Encoding(false);
            Console.OutputEncoding = new System.Text.UTF8Encoding(false);
            string text = Console.In.ReadToEnd();
            var settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null };
            var document = new XmlDocument { XmlResolver = null };
            using (var reader = XmlReader.Create(new StringReader(text), settings)) document.Load(reader);
            foreach (XmlAttribute attribute in document.SelectNodes("//@RegularExpression | //@ZoneFilterRegularExpression | //@FfxivZoneFilterRegularExpression"))
                new Regex(attribute.Value, RegexOptions.None, TimeSpan.FromSeconds(1));
            using (var reader = XmlReader.Create(new StringReader(text), settings))
            {
                var pack = (TriggernometryExport)new XmlSerializer(typeof(TriggernometryExport)).Deserialize(reader);
                if (pack.ExportedFolder == null) throw new ArgumentException("A folder export is required");
            }
            Console.WriteLine("valid");
            return 0;
        }
        catch (Exception exception)
        {
            while (exception.InnerException != null) exception = exception.InnerException;
            Console.Error.WriteLine(exception.Message);
            return 1;
        }
    }
}
