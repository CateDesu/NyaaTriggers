// Phase 3 - combatant/entity bridge for headless triggernometry-core (DECISION 2: up front).
//
// Triggernometry's ${_me}/${_ffxiv*}/${_entity[..]} grammar + the BridgeFFXIV.GetMyself/GetAllEntities script
// accessors all funnel through RealPlugin.InstanceHook -> a PluginWrapper{pluginObj} that BridgeFFXIV reflects:
//   pluginObj.DataRepository.GetCurrentPlayerID() / .GetCombatantList() / .GetCurrentFFXIVProcess()  (modern path)
//   pluginObj.DataSubscription.ZoneChanged  (a void(uint,string) event)
// Each combatant is read via C# `dynamic` in BridgeFFXIV.PopulateClumpFromCombatant, so member NAMES+TYPES must
// match. This provides those shapes from data fed by IINACT (no real FFXIV_ACT_Plugin / no process memory needed).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using Triggernometry;

// A combatant POCO matching the member surface BridgeFFXIV.PopulateClumpFromCombatant reads via `dynamic`.
// Types mirror FFXIV_ACT_Plugin.Common.Models.Combatant so the dynamic operator/overload resolution matches prod.
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
    public byte PartyType { get; set; }   // 0=none, 1=party, 2=alliance
    public IntPtr Address { get; set; } = IntPtr.Zero;   // unavailable on Linux network feed
    public byte Job { get; set; }
}

public delegate void FakeZoneChangedDelegate(uint zoneId, string zoneName);

// pluginObj.DataSubscription - only ZoneChanged is reflected (event must be void(uint,string)).
public sealed class FakeSubscription
{
    public event FakeZoneChangedDelegate ZoneChanged;
    public void RaiseZoneChanged(uint zoneId, string zoneName) { var h = ZoneChanged; if (h != null) h(zoneId, zoneName); }
}

// pluginObj.DataRepository - the modern path BridgeFFXIV.GetCombatants uses (skips the legacy memory field-walk).
public sealed class FakeRepo
{
    public uint GetCurrentPlayerID() => CombatantBridge.PlayerId;
    public List<FakeCombatant> GetCombatantList() => CombatantBridge.Snapshot();  // FRESH snapshot each call (engine foreaches without locking the backing list)
    public Process GetCurrentFFXIVProcess() => null;                              // no real game process on Linux
}

// The fake FFXIV_ACT_Plugin instance handed back via InstanceHook.
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

    // Atomically swap in a fresh combatant snapshot (later: fed from the IINACT combatant feed).
    public static void SetSnapshot(uint playerId, FakeCombatant[] combatants)
    {
        PlayerId = playerId;
        _snapshot = combatants ?? new FakeCombatant[0];
    }

    public static List<FakeCombatant> Snapshot() => new List<FakeCombatant>(_snapshot);

    public static void RaiseZoneChanged(uint zoneId, string zoneName)
    {
        // Set the static directly too: the first 01| can arrive before the engine subscribes its
        // ZoneChanged handler (attached lazily on the worker thread), which would drop the event.
        Triggernometry.PluginBridges.BridgeFFXIV.ZoneID = zoneId;
        _fake.DataSubscription.RaiseZoneChanged(zoneId, zoneName);
    }

    // Always state=1 with the fake instance; an empty snapshot just yields a null Myself / empty entities (no NPE).
    public static RealPlugin.PluginWrapper Instance() =>
        new RealPlugin.PluginWrapper { pluginObj = _fake, state = 1, fileversion = "0.0.0.0", expectedversion = "0.0.0.0" };
}
