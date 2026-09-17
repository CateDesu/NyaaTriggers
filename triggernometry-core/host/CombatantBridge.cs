// Expose IINACT combatants through the plugin members Triggernometry reflects.
// DataRepository supplies player and combatant data, and DataSubscription supplies zone
// changes. Member names and types must match the engine's dynamic access.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using Triggernometry;

// Match the combatant member names and types used by BridgeFFXIV dynamic access.
public sealed class FakeCombatant
{
    public string Name { get; set; } = "";
    public uint CurrentHP { get; set; }
    public uint CurrentMP { get; set; }
    public uint CurrentGP { get; set; }
    public uint CurrentCP { get; set; }
    public uint MaxHP { get; set; }
    public uint MaxMP { get; set; }
    public uint MaxGP { get; set; }
    public uint MaxCP { get; set; }
    public byte Level { get; set; }
    public float PosX { get; set; }
    public float PosY { get; set; }
    public float PosZ { get; set; }
    public uint ID { get; set; }
    public bool IsCasting { get; set; }
    public uint CastTargetID { get; set; }
    public uint TargetID { get; set; }
    public float CastDurationCurrent { get; set; }
    public float CastDurationMax { get; set; }
    public uint CastBuffID { get; set; }
    public float Heading { get; set; }
    public byte EffectiveDistance { get; set; }
    public uint WorldID { get; set; }
    public string WorldName { get; set; } = "";
    public uint CurrentWorldID { get; set; }
    public uint OwnerID { get; set; }
    public uint BNpcNameID { get; set; }
    public uint BNpcID { get; set; }
    public byte PartyType { get; set; }   // Zero for none, one for party, two for alliance.
    public IntPtr Address { get; set; } = IntPtr.Zero;   // unavailable on Linux network feed
    public byte Job { get; set; }
}

public delegate void FakeZoneChangedDelegate(uint zoneId, string zoneName);

// ZoneChanged must retain the reflected uint and string event signature.
public sealed class FakeSubscription
{
    public event FakeZoneChangedDelegate ZoneChanged;
    public void RaiseZoneChanged(uint zoneId, string zoneName) { var h = ZoneChanged; if (h != null) h(zoneId, zoneName); }
}

public sealed class FakeRepo
{
    public uint GetCurrentPlayerID() => CombatantBridge.PlayerId;
    // The engine iterates without locking, so return a fresh list.
    public List<FakeCombatant> GetCombatantList() => CombatantBridge.Snapshot();
    public Process GetCurrentFFXIVProcess() => null;
}

public sealed class FakeActPlugin
{
    public FakeRepo DataRepository { get; } = new FakeRepo();
    public FakeSubscription DataSubscription { get; } = new FakeSubscription();
}

public static class CombatantBridge
{
    static volatile FakeCombatant[] _snapshot = new FakeCombatant[0];
    public static uint PlayerId;
    static readonly FakeActPlugin _fake = new FakeActPlugin();

    public static FakeActPlugin Plugin => _fake;

    public static void SetSnapshot(uint playerId, FakeCombatant[] combatants)
    {
        PlayerId = playerId;
        _snapshot = combatants ?? new FakeCombatant[0];
    }

    public static List<FakeCombatant> Snapshot() => new List<FakeCombatant>(_snapshot);

    public static void RaiseZoneChanged(uint zoneId, string zoneName)
    {
        // Update the static zone too because the first zone event may arrive before the
        // worker subscribes.
        Triggernometry.PluginBridges.BridgeFFXIV.ZoneID = zoneId;
        _fake.DataSubscription.RaiseZoneChanged(zoneId, zoneName);
    }

    // Keep the bridge available with an empty snapshot. Player lookup then returns
    // null.
    public static RealPlugin.PluginWrapper Instance() =>
        new RealPlugin.PluginWrapper { pluginObj = _fake, state = 1, fileversion = "0.0.0.0", expectedversion = "0.0.0.0" };
}
