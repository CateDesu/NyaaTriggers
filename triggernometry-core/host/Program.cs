// triggernometry-core - Strategy-A Mono stub host for NyaaTriggers.
//
// Boots the REAL Triggernometry engine headless under Mono+Xvfb (no engine source patch), runs ALL triggers
// (including Roslyn ExecuteScript) against a fed log stream, and streams resolved callouts back as JSON.
//
// SERVER mode (the real sidecar):   xvfb-run -a mono triggernometry-core.exe <cfgDir> --serve [packPath...]
//   stdin : one JSON object per line  {"t":"log","line":"21|..."} | {"t":"zone","id":N,"name":".."}
//                                     | {"t":"combatants","me":ID,"list":[{combatant}...]}
//   stdout: one JSON object per line  {"t":"callout","tts":".."} | {"t":"sound",..} | {"t":"status","active":bool}
//
// TEST mode (regression):           xvfb-run -a mono triggernometry-core.exe <cfgDir> <packPath.xml> [testLogLine]
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Threading;
using System.Windows.Forms;
using System.Xml.Serialization;
using Triggernometry;

static class Program
{
    static volatile bool crashed = false;
    static volatile string crashMsg = null;
    static volatile int calloutCount = 0;
    static RealPlugin plug;
    static string currentZone = "";

    // Callout inventory + per-callout edit/disable. Maps a stable id (triggerGuid#ttsIndex) to the LIVE
    // UseTTS Action object the engine fires; editing/disabling rewrites that action's text in place.
    static readonly Dictionary<string, Triggernometry.Action> _calloutActions = new Dictionary<string, Triggernometry.Action>();
    static readonly Dictionary<string, string> _calloutOriginal = new Dictionary<string, string>();
    static readonly Dictionary<string, string> _calloutOverride = new Dictionary<string, string>();
    static readonly HashSet<string> _calloutDisabled = new HashSet<string>();
    static readonly object _coLock = new object();

    [STAThread]
    static void Main(string[] args)
    {
        AppDomain.CurrentDomain.UnhandledException += (s, e) => { crashed = true; crashMsg = Convert.ToString(e.ExceptionObject); Err("[FATAL] " + crashMsg); };
        Application.ThreadException += (s, e) => { crashed = true; crashMsg = Convert.ToString(e.Exception); Err("[FATAL/UI] " + crashMsg); };
        // Pin stdio to UTF-8 so non-ASCII FFXIV text (combatant/zone names) round-trips regardless of locale.
        try { var u8 = new System.Text.UTF8Encoding(false); Console.InputEncoding = u8; Console.OutputEncoding = u8; }
        catch (Exception ex) { Err("[host] set UTF-8 console: " + ex.Message); }
        try { Application.EnableVisualStyles(); } catch (Exception ex) { Err("[host] EnableVisualStyles: " + ex.Message); }

        // ---- arg parsing ----
        string cfgDir = (args.Length > 0 && args[0].Length > 0) ? args[0] : Path.Combine(Path.GetTempPath(), "tnc");
        bool serve = Array.IndexOf(args, "--serve") >= 0;
        var packPaths = new List<string>();
        string testLine = "00|2026-06-27T12:00:00.0000000+00:00|0839|Spike|SPIKETEST please call|0000abcd";
        for (int i = 1; i < args.Length; i++)
        {
            if (args[i] == "--serve") continue;
            if (args[i].EndsWith(".xml")) packPaths.Add(args[i]);
            else testLine = args[i];
        }
        Directory.CreateDirectory(cfgDir);
        const string pluginName = "Triggernometry";
        string engineDir = AppDomain.CurrentDomain.BaseDirectory;
        Err("[host] mode=" + (serve ? "SERVE" : "TEST") + " cfgDir=" + cfgDir + " packs=" + packPaths.Count);

        BuildConfig(Path.Combine(cfgDir, pluginName + ".config.xml"), packPaths);

        // ---- hidden mainform + hosted TabPage with forced handles ----
        Form mainform = new Form { ShowInTaskbar = false, FormBorderStyle = FormBorderStyle.None };
        mainform.Load += (s, e) => ((Form)s).Visible = false;
        var _h = mainform.Handle; mainform.CreateControl();
        Form tabHost = new Form { ShowInTaskbar = false, FormBorderStyle = FormBorderStyle.None };
        TabControl tc = new TabControl(); tabHost.Controls.Add(tc);
        TabPage tp = new TabPage("Triggernometry"); tc.TabPages.Add(tp);
        var _h2 = tabHost.Handle; tabHost.CreateControl();

        // ---- bring up the engine ----
        RealPlugin.ResetPlugin();
        plug = RealPlugin.plug;
        plug.mainform = mainform;
        plug.pluginName = pluginName;
        plug.path = cfgDir;
        plug.pluginPath = engineDir;

        RealPlugin.InstanceHook    = (n, t) => CombatantBridge.Instance();
        plug.ActInitedHook         = () => true;
        plug.InCombatHook          = () => false;
        plug.CurrentZoneHook       = () => currentZone;
        plug.ActiveEncounterHook   = () => "";
        plug.LastEncounterHook     = () => "";
        plug.EncounterDurationHook  = () => 0.0;
        plug.TtsPlaybackHook       = (text) => { if (string.IsNullOrWhiteSpace(text)) return; Interlocked.Increment(ref calloutCount); Out("{\"t\":\"callout\",\"tts\":" + J(text) + "}"); };
        plug.SoundPlaybackHook     = (file, vol) => Out("{\"t\":\"sound\",\"file\":" + J(file) + ",\"volume\":" + vol + "}");
        plug.SetCombatStateHook    = b => { };
        plug.LogAllNetworkHook     = b => { };
        plug.UseDeucalionHook      = b => { };
        plug.ACTEncounterLogHook   = m => { };
        plug.CornerShowHook        = () => { };
        plug.CornerHideHook        = () => { };

        if (!serve)
            CombatantBridge.SetSnapshot(0x10001234u, new[] {
                new FakeCombatant { ID = 0x10001234u, Name = "Spike Player", PartyType = 1, Job = 19,
                                    CurrentHP = 50000, MaxHP = 50000, Level = 100, PosX = 12.5f, PosY = 7.25f, PosZ = 0f, Heading = 1.5f } });

        Err("[host] calling InitPlugin...");
        var statusLabel = new Label();
        Exception initEx = null;
        try { plug.InitPlugin(tp, statusLabel); }
        catch (Exception ex) { initEx = ex; Err("[host] InitPlugin THREW: " + ex); }
        bool initOk = ReadIsInitialized(plug) && initEx == null;
        Err("[host] isInitialized=" + initOk + "  status='" + statusLabel.Text + "'  registeredTriggers=" + ReadTriggerCount(plug));

        if (!initOk)
        {
            EmitStatus(false, "init failed: " + statusLabel.Text);
            Environment.Exit(2);
        }
        EmitStatus(true, "ready");
        BuildAndEmitInventory();   // report the editable callouts (UseTTS) to NyaaTriggers

        if (serve) RunServer();
        else RunTest(packPaths.Count > 0, testLine);
    }

    // ---------------- SERVER MODE ----------------
    static void RunServer()
    {
        var reader = new Thread(() =>
        {
            try
            {
                string line;
                while ((line = Console.In.ReadLine()) != null)
                {
                    if (line.Length == 0) continue;
                    try { Dispatch(line); }
                    catch (Exception ex) { Err("[host] dispatch error: " + ex.Message + " on: " + Truncate(line, 200)); }
                }
            }
            catch (Exception ex) { Err("[host] stdin loop ended: " + ex.Message); }
            finally
            {
                // The engine's worker threads are FOREGROUND (RealPlugin.cs:2353-2361), so Main returning
                // would NOT end the process -> mono+Xvfb orphan. Force exit on stdin EOF. This also covers
                // the EOF-before-Application.Run startup race.
                EmitStatus(false, "stopped");
                try { Application.Exit(); } catch { }
                Environment.Exit(0);
            }
        }) { IsBackground = true, Name = "stdin-reader" };
        reader.Start();
        Err("[host] server ready; reading IINACT messages on stdin");
        Application.Run();
    }

    static void Dispatch(string json)
    {
        using (var doc = JsonDocument.Parse(json))
        {
            JsonElement root = doc.RootElement, el;
            string t = root.TryGetProperty("t", out el) ? el.GetString() : null;
            switch (t)
            {
                case "log":
                    if (root.TryGetProperty("line", out el))
                    {
                        string raw = el.GetString() ?? "";
                        if (raw.Length == 0) return;
                        if (raw.StartsWith("01|"))   // ChangeZone: 01|ts|<zoneId hex>|<zoneName>|hash
                        {
                            var f = raw.Split(new[] { '|' });   // char[] overload: Split(char,opts) is Mono/.NET-Core only and throws MissingMethodException on .NET Framework (Windows)
                            if (f.Length > 3)
                            {
                                currentZone = f[3];
                                uint zoneId;
                                uint.TryParse(f[2], System.Globalization.NumberStyles.HexNumber,
                                              System.Globalization.CultureInfo.InvariantCulture, out zoneId);
                                CombatantBridge.RaiseZoneChanged(zoneId, currentZone);   // drives ${_ffxivzoneid} + name filters
                            }
                        }
                        // ACT fires BeforeLogLineRead first, plugins may rewrite the
                        // line there, then OnLogLineRead. Keep ACT's order so
                        // same-line side effects behave like the real thing.
                        plug.BeforeLogLineRead(false, raw, currentZone);
                        plug.OnLogLineRead(false, raw, currentZone);
                    }
                    break;
                case "zone":
                    if (root.TryGetProperty("name", out el)) currentZone = el.GetString() ?? "";
                    CombatantBridge.RaiseZoneChanged(JU(root, "id"), currentZone);
                    break;
                case "combatants":
                    uint me = JU(root, "me");
                    var list = new List<FakeCombatant>();
                    if (root.TryGetProperty("list", out el) && el.ValueKind == JsonValueKind.Array)
                        foreach (var c in el.EnumerateArray()) list.Add(ParseCombatant(c));
                    CombatantBridge.SetSnapshot(me, list.ToArray());
                    break;
                case "set_callout":   // edit one callout's spoken text live (id, text); text=null reverts to default
                    {
                        string id = root.TryGetProperty("id", out el) ? el.GetString() : null;
                        if (id != null)
                        {
                            string txt = root.TryGetProperty("text", out var te2) && te2.ValueKind == JsonValueKind.String ? te2.GetString() : null;
                            lock (_coLock) { if (txt == null) _calloutOverride.Remove(id); else _calloutOverride[id] = txt; }
                            ApplyCallout(id);
                        }
                    }
                    break;
                case "set_disabled":  // the full set of callout ids to suppress (rest re-enabled)
                    {
                        var ids = new HashSet<string>();
                        if (root.TryGetProperty("ids", out el) && el.ValueKind == JsonValueKind.Array)
                            foreach (var x in el.EnumerateArray()) { var s = x.GetString(); if (s != null) ids.Add(s); }
                        List<string> all;
                        lock (_coLock) { all = new List<string>(_calloutActions.Keys); _calloutDisabled.Clear(); foreach (var i in ids) _calloutDisabled.Add(i); }
                        foreach (var i in all) ApplyCallout(i);
                    }
                    break;
                case "ping": Out("{\"t\":\"pong\"}"); break;
                default: break;
            }
        }
    }

    static uint JU(JsonElement c, string k) { JsonElement v; uint n; return (c.TryGetProperty(k, out v) && v.ValueKind == JsonValueKind.Number && v.TryGetUInt32(out n)) ? n : 0u; }
    static float JF(JsonElement c, string k) { JsonElement v; float n; return (c.TryGetProperty(k, out v) && v.ValueKind == JsonValueKind.Number && v.TryGetSingle(out n)) ? n : 0f; }
    static byte JB(JsonElement c, string k) { return (byte)Math.Min(JU(c, k), 255u); }
    static string JS(JsonElement c, string k) { JsonElement v; return c.TryGetProperty(k, out v) ? (v.GetString() ?? "") : ""; }

    // Walk the LIVE registered triggers, register each UseTTS callout (id = triggerGuid#ttsIndex -> Action),
    // and emit the inventory so NyaaTriggers can list / edit / toggle them.
    static void BuildAndEmitInventory()
    {
        var sb = new System.Text.StringBuilder();
        sb.Append("{\"t\":\"inventory\",\"triggers\":[");
        bool first = true;
        try
        {
            var fi = typeof(RealPlugin).GetField("Triggers", BindingFlags.NonPublic | BindingFlags.Instance);
            var triggers = fi != null ? fi.GetValue(plug) as System.Collections.IEnumerable : null;
            var parentProp = typeof(Triggernometry.Trigger).GetProperty("Parent", BindingFlags.NonPublic | BindingFlags.Instance);
            if (triggers != null)
                foreach (Triggernometry.Trigger t in triggers)
                {
                    if (t == null || t.Actions == null) continue;
                    string fight = "";
                    try { var fo = parentProp != null ? parentProp.GetValue(t) as Triggernometry.Folder : null; if (fo != null) fight = fo.Name ?? ""; } catch { }
                    int idx = 0;
                    foreach (var a in t.Actions)
                    {
                        if (a == null || a.ActionType != "UseTTS") continue;
                        string text = a.UseTTSTextExpression;   // property getter returns null when empty
                        if (string.IsNullOrEmpty(text)) continue;
                        string id = t.Id.ToString() + "#" + idx;
                        lock (_coLock) { _calloutActions[id] = a; if (!_calloutOriginal.ContainsKey(id)) _calloutOriginal[id] = text; }
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"id\":").Append(J(id)).Append(",\"name\":").Append(J(t.Name ?? ""))
                          .Append(",\"fight\":").Append(J(fight)).Append(",\"text\":").Append(J(text)).Append('}');
                        idx++;
                    }
                }
        }
        catch (Exception ex) { Err("[host] inventory build error: " + ex.Message); }
        sb.Append("]}");
        Out(sb.ToString());
    }

    // Re-apply the current edit/disable state to one callout's live Action text.
    static void ApplyCallout(string id)
    {
        Triggernometry.Action a; string val;
        lock (_coLock)
        {
            if (!_calloutActions.TryGetValue(id, out a)) return;
            if (_calloutDisabled.Contains(id)) val = "";
            else if (!_calloutOverride.TryGetValue(id, out val))
                val = _calloutOriginal.TryGetValue(id, out var o) ? o : null;
        }
        try { a.UseTTSTextExpression = val ?? ""; } catch (Exception ex) { Err("[host] ApplyCallout: " + ex.Message); }
    }

    static FakeCombatant ParseCombatant(JsonElement c)
    {
        return new FakeCombatant
        {
            ID = JU(c, "id"), Name = JS(c, "name"), Job = JB(c, "job"), Level = JB(c, "level"), PartyType = JB(c, "party"),
            CurrentHP = JU(c, "hp"), MaxHP = JU(c, "maxhp"), CurrentMP = JU(c, "mp"), MaxMP = JU(c, "maxmp"),
            PosX = JF(c, "x"), PosY = JF(c, "y"), PosZ = JF(c, "z"), Heading = JF(c, "h"),
            TargetID = JU(c, "targetid"), OwnerID = JU(c, "ownerid"), BNpcID = JU(c, "bnpcid"), BNpcNameID = JU(c, "bnpcnameid"),
            WorldID = JU(c, "worldid"), CurrentWorldID = JU(c, "worldid"), WorldName = JS(c, "worldname"),
            IsCasting = JU(c, "castid") > 0, CastBuffID = JU(c, "castid"), CastTargetID = JU(c, "casttargetid"),
            CastDurationCurrent = JF(c, "casttime"), CastDurationMax = JF(c, "maxcasttime"),
            EffectiveDistance = JB(c, "distance"),
        };
    }

    // ---------------- TEST MODE ----------------
    static void RunTest(bool feed, string testLine)
    {
        if (feed)
        {
            var feeder = new Thread(() =>
            {
                Thread.Sleep(1500);
                Err("[host] feeding: " + testLine);
                try { plug.BeforeLogLineRead(false, testLine, "SpikeZone"); plug.OnLogLineRead(false, testLine, "SpikeZone"); }
                catch (Exception ex) { Err("[host] feed ex: " + ex); }
            }) { IsBackground = true };
            feeder.Start();
        }
        var timer = new System.Windows.Forms.Timer { Interval = feed ? 6000 : 3000 };
        timer.Tick += (s, e) => { timer.Stop(); Application.Exit(); };
        timer.Start();
        Application.Run();
        try { var sv = Triggernometry.Interpreter.StaticHelpers.GetScalarVariable(false, "spikevar");
              if (!string.IsNullOrEmpty(sv)) Err("[host] script var spikevar='" + sv + "'"); } catch { }
        Err("[host] RESULT callouts=" + calloutCount + " crashed=" + crashed);
        Err("[host] " + (calloutCount > 0 && !crashed ? "CALLOUT: PASS" : "CALLOUT: FAIL"));
        Environment.Exit(calloutCount > 0 && !crashed ? 0 : 1);
    }

    // ---------------- helpers ----------------
    static bool ReadIsInitialized(RealPlugin p)
    {
        var pi = typeof(RealPlugin).GetProperty("isInitialized", BindingFlags.NonPublic | BindingFlags.Instance);
        try { return pi != null && (bool)pi.GetValue(p, null); } catch { return false; }
    }
    static int ReadTriggerCount(RealPlugin p)
    {
        try { var fi = typeof(RealPlugin).GetField("Triggers", BindingFlags.NonPublic | BindingFlags.Instance);
              var l = fi != null ? fi.GetValue(p) as System.Collections.ICollection : null; return l != null ? l.Count : -1; }
        catch { return -1; }
    }

    static void BuildConfig(string file, List<string> packPaths)
    {
        var c = new Configuration
        {
            UseScarborough = false, WindowToMonitor = "",
            TtsMethod = Configuration.AudioRoutingMethodEnum.ACT, SoundMethod = Configuration.AudioRoutingMethodEnum.ACT,
            StartEndpointOnLaunch = false,
            UpdateNotifications = Configuration.UpdateNotificationsEnum.No, DefaultRepository = Configuration.UpdateNotificationsEnum.No,
            DebugLevel = RealPlugin.DebugLevelEnum.Info,
        };
        foreach (var packPath in packPaths)
        {
            try
            {
                var tex = TriggernometryExport.Unserialize(File.ReadAllText(packPath));
                if (tex == null || tex.Corrupted || tex.ExportedFolder == null) { Err("[host] WARN pack failed: " + packPath); continue; }
                if (c.Root.Folders == null) c.Root.Folders = new List<Folder>();
                c.Root.Folders.Add(tex.ExportedFolder);
                int n = FixupExecuteScriptAssemblies(tex.ExportedFolder);
                Err("[host] grafted '" + tex.ExportedFolder.Name + "' (" + (tex.ExportedFolder.Triggers != null ? tex.ExportedFolder.Triggers.Count : 0) + " triggers)" + (n > 0 ? " [fixed " + n + " empty-asm script(s)]" : ""));
            }
            catch (Exception ex) { Err("[host] pack load error " + packPath + ": " + ex.Message); }
        }
        var xs = new XmlSerializer(typeof(Configuration));
        var settings = new System.Xml.XmlWriterSettings { Encoding = new System.Text.UTF8Encoding(false), Indent = true };
        using (var fs = new FileStream(file, FileMode.Create, FileAccess.Write))
        using (var xw = System.Xml.XmlWriter.Create(fs, settings)) xs.Serialize(xw, c);
    }

    // ExecuteScript actions default _ExecScriptAssembliesExpression="" -> Interpreter.Evaluate does AddReferences("")
    // -> ArgumentException, script never runs. Give empty ones a valid loaded assembly (keeps the engine unpatched).
    static int FixupExecuteScriptAssemblies(Folder f)
    {
        if (f == null) return 0;
        int n = 0;
        if (f.Triggers != null)
            foreach (var t in f.Triggers)
                if (t != null && t.Actions != null)
                    foreach (var a in t.Actions)
                        if (a != null && a.ActionType == "ExecuteScript" && string.IsNullOrWhiteSpace(a.ExecScriptAssembliesExpression))
                        { a.ExecScriptAssembliesExpression = "TriggernometryPlugin"; n++; }
        if (f.Folders != null) foreach (var sub in f.Folders) n += FixupExecuteScriptAssemblies(sub);
        return n;
    }

    static readonly object _ol = new object();
    static void Out(string s) { lock (_ol) { Console.Out.WriteLine(s); Console.Out.Flush(); } }
    static void Err(string s) { lock (_ol) { Console.Error.WriteLine(s); Console.Error.Flush(); } }
    static void EmitStatus(bool active, string msg) { Out("{\"t\":\"status\",\"active\":" + (active ? "true" : "false") + ",\"msg\":" + J(msg) + "}"); }
    static string Truncate(string s, int n) { return s.Length <= n ? s : s.Substring(0, n); }
    static string J(string s)
    {
        if (string.IsNullOrEmpty(s)) return "\"\"";
        var sb = new System.Text.StringBuilder(s.Length + 2);
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"': sb.Append("\\\""); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
        return sb.ToString();
    }
}
