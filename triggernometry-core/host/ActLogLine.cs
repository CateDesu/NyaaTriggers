using System;
using System.Collections.Generic;
using System.Globalization;

static class ActLogLine
{
    // ACT names and field order are documented in cactbot's LogGuide.
    // https://github.com/OverlayPlugin/cactbot/blob/main/docs/LogGuide.md
    static readonly Dictionary<int, string> Names = new Dictionary<int, string>
    {
        { 0, "ChatLog" }, { 1, "Territory" }, { 2, "ChangePrimaryPlayer" },
        { 3, "AddCombatant" }, { 4, "RemoveCombatant" }, { 11, "PartyList" },
        { 12, "PlayerStats" }, { 20, "StartsCasting" }, { 21, "ActionEffect" },
        { 22, "AOEActionEffect" }, { 23, "CancelAction" }, { 24, "DoTHoT" },
        { 25, "Death" }, { 26, "StatusAdd" }, { 27, "TargetIcon" },
        { 28, "WaymarkMarker" }, { 29, "SignMarker" }, { 30, "StatusRemove" },
        { 31, "Gauge" }, { 32, "World" }, { 33, "Director" },
        { 34, "NameToggle" }, { 35, "Tether" }, { 36, "LimitBreak" },
        { 37, "EffectResult" }, { 38, "StatusList" }, { 39, "UpdateHp" },
        { 40, "ChangeMap" }, { 41, "SystemLogMessage" }, { 42, "StatusList3" },
        { 43, "StatusEffectListForay3" }, { 249, "Settings" }, { 250, "Process" },
        { 251, "Debug" }, { 252, "PacketDump" }, { 253, "Version" }, { 254, "Error" },
    };

    public static string Format(string raw)
    {
        if (string.IsNullOrEmpty(raw)) return raw;
        var fields = raw.Split(new[] { '|' });
        int type;
        DateTimeOffset timestamp;
        if (fields.Length < 4
            || !int.TryParse(fields[0], NumberStyles.None, CultureInfo.InvariantCulture, out type)
            || !DateTimeOffset.TryParse(fields[1], CultureInfo.InvariantCulture,
                                       DateTimeStyles.None, out timestamp)) return raw;

        string name;
        if (!Names.TryGetValue(type, out name)) name = type.ToString(CultureInfo.InvariantCulture);
        // The final network field is the wire checksum and is absent in ACT logs.
        string body = string.Join(":", fields, 2, fields.Length - 3);
        return "[" + timestamp.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture) + "] "
            + name + " " + type.ToString("X2", CultureInfo.InvariantCulture) + ":" + body;
    }
}
