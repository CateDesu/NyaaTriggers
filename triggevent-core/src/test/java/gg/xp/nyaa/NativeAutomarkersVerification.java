package gg.xp.nyaa;

import gg.xp.telestosupport.TelestoMain;
import gg.xp.xivdata.data.Job;
import gg.xp.xivsupport.events.triggers.duties.Dragonsong;
import gg.xp.xivsupport.events.triggers.duties.ewult.OmegaUltimate;
import gg.xp.xivsupport.events.triggers.duties.ewult.omega.DynamisDeltaAssignment;
import gg.xp.xivsupport.events.triggers.jails.JailSolver;
import gg.xp.xivsupport.events.triggers.marks.adv.MarkerSign;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;

public final class NativeAutomarkersVerification {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static NativeAutomarkers adapter;
    private static MutablePicoContainer pico;

    public static void main(String[] args) {
        try {
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            pico = (MutablePicoContainer) boot.invoke(null);
            adapter = new NativeAutomarkers(pico);
            inventory();
            noOp();
            invalid();
            explicitInitialOverride();
            priorities();
            maps();
            delays();
            System.out.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            error.printStackTrace();
            System.exit(1);
        }
    }

    private static void inventory() {
        JsonNode inventory = tree(adapter.inventory());
        check(inventory.path("mechanics").size() == 13, "Incomplete mechanic inventory");
        check(inventory.path("settings").size() == 46, "Incomplete settings inventory");
        Map<String, Long> counts = new LinkedHashMap<>();
        inventory.path("settings").forEach(setting -> counts.merge(setting.path("type").asText(), 1L, Long::sum));
        check(counts.equals(Map.of("boolean", 23L, "integer", 6L, "jobs", 9L, "marker_map", 8L)), "Wrong settings types: " + counts);
        Set<String> ids = new java.util.HashSet<>();
        inventory.path("settings").forEach(setting -> check(ids.add(setting.path("id").asText()), "Duplicate setting ID"));
        inventory.path("mechanics").forEach(mechanic -> mechanic.path("settings").forEach(id -> check(ids.contains(id.asText()), "Unknown mechanic setting")));
        check(setting("uwu.enabled").path("default").booleanValue(), "UWU default changed");
        check(!setting("top.sigma.enabled").path("default").booleanValue(), "TOP enabled by import");
        check(setting("native_umad").path("managed").asText().equals("frontend"), "UMAD ownership gate bypassed");
        check(setting("uwu.clear_delay_ms").path("min").isMissingNode() && setting("uwu.clear_delay_ms").path("max").isMissingNode(), "Invented UWU bounds");
        check(setting("top.sigma.delay_seconds").path("min").intValue() == 0 && setting("top.sigma.delay_seconds").path("max").intValue() == 50, "Sigma native bounds changed");
        check(inventory.path("markers").size() == MarkerSign.values().length, "Marker choices lost native signs");
        check(inventory.path("jobs").size() == java.util.Arrays.stream(Job.values()).filter(Job::isCombatJob).count(), "Job choices lost a valid job");
        inventory.path("settings").forEach(setting -> {
            if (!setting.has("managed")) check(setting.path("value").equals(setting.path("default")), "Fresh native default disagrees with inventory: " + setting.path("id"));
            if (!setting.path("type").asText().equals("marker_map")) return;
            Set<String> slots = new java.util.HashSet<>();
            setting.path("slots").forEach(slot -> slots.add(slot.path("id").asText()));
            check(setting.path("value").propertyNames().equals(slots), "Current map omitted slots");
            check(setting.path("default").propertyNames().equals(slots), "Default map omitted slots");
            setting.path("presets").forEach(preset -> check(preset.path("value").propertyNames().equals(slots), "Preset omitted slots"));
        });
        System.out.println("VERIFIED 13 output sets, 46 settings, 8 maps, native defaults, signs, jobs and presets");
    }

    private static void noOp() {
        Map<String, Object> values = new LinkedHashMap<>();
        tree(adapter.inventory()).path("settings").forEach(setting -> {
            if (!setting.has("managed")) values.put(setting.path("id").asText(), setting.path("value"));
        });
        NativeAutomarkers.Plan plan = adapter.plan(tree(values));
        check(!plan.changed(), "Identical configuration changes marker lifetime");
        AtomicInteger writes = new AtomicInteger();
        plan.apply(write -> { writes.incrementAndGet(); write.run(); });
        check(writes.get() == 0, "No-op configuration wrote settings");
        check(!adapter.plan(null).changed(), "Absent settings were changed");
        System.out.println("VERIFIED complete identical replay has no changes or setting writes");
    }

    private static void invalid() {
        String before = MAPPER.writeValueAsString(adapter.inventory());
        List<Map<String, Object>> invalid = List.of(
                Map.of("unknown", true), Map.of("native_umad", false), Map.of("top.sigma.enabled", 1),
                Map.of("top.sigma.delay_seconds", -1), Map.of("top.sigma.delay_seconds", 51),
                Map.of("top.sigma.delay_seconds", 1.5), Map.of("uwu.clear_delay_ms", new BigInteger("9223372036854775808")),
                Map.of("top.priority", List.of("DRG")), Map.of("top.priority", List.of("DRG", "DRG")),
                Map.of("top.priority", List.of("NO_JOB")), Map.of("top.delta.markers", Map.of("NearWorld", Map.of("enabled", true, "marker", "CROSS"))),
                Map.of("top.delta.markers", Map.of("NearWorld", Map.of("enabled", true, "marker", "WRONG"), "DistantWorld", Map.of("enabled", true, "marker", "CROSS"))),
                Map.of("top.delta.markers", Map.of("NearWorld", Map.of("enabled", 1, "marker", "CROSS"), "DistantWorld", Map.of("enabled", true, "marker", "CROSS"))),
                Map.of("top.delta.markers", Map.of("NearWorld", Map.of("enabled", true, "marker", "CROSS", "other", 1), "DistantWorld", Map.of("enabled", true, "marker", "CROSS")))
        );
        for (Map<String, Object> wrong : invalid) {
            Map<String, Object> combined = new LinkedHashMap<>();
            combined.put("uwu.enabled", false);
            combined.putAll(wrong);
            boolean rejected = false;
            try { adapter.plan(tree(combined)).apply(Runnable::run); }
            catch (IllegalArgumentException error) { rejected = true; }
            check(rejected, "Invalid settings were accepted: " + wrong);
            check(before.equals(MAPPER.writeValueAsString(adapter.inventory())), "Invalid batch partly changed live settings");
        }
        for (JsonNode wrong : List.of(tree(List.of()), tree(null))) {
            boolean rejected = false;
            try { adapter.plan(wrong); }
            catch (IllegalArgumentException error) { rejected = true; }
            check(rejected, "Non-object settings accepted");
        }
        System.out.println("VERIFIED 16 invalid batches reject before any live mutation including managed UMAD");
    }

    private static void priorities() {
        OmegaUltimate top = pico.getComponent(OmegaUltimate.class);
        List<Job> defaults = List.copyOf(top.getGroupPrioJobSort().getDefaultJobOrder());
        List<Job> parent = new ArrayList<>(defaults);
        Collections.reverse(parent);
        List<Job> own = new ArrayList<>(defaults);
        Collections.rotate(own, 3);
        apply(Map.of("top.priority", names(parent), "top.p1.priority", names(own)));
        check(top.getP1prio().getOwnJobOrder().equals(own), "Disabled override lost retained order");
        check(top.getP1prio().getJobOrder().equals(parent), "Disabled override did not follow parent");
        check(setting("top.p1.priority").path("value").equals(tree(names(own))), "Inventory hid retained order");
        check(setting("top.p1.priority").path("effective_order").equals(tree(names(parent))), "Inventory hid parent fallback");
        check(top.getGroupPrioJobSort().getDefaultJobOrder().equals(defaults), "Reading defaults changed with saved order");
        check(top.getP1prio().getDefaultJobOrder().equals(parent), "Override reset did not inherit parent");
        apply(Map.of("top.p1.priority_override", true));
        check(top.getP1prio().getJobOrder().equals(own), "Enabling override lost chosen order");
        apply(Map.of("top.p1.priority_override", false));
        check(top.getP1prio().getOwnJobOrder().equals(own) && top.getP1prio().getJobOrder().equals(parent), "Disabling override lost own order");
        apply(Map.of("top.p1.priority_override", true));
        check(top.getP1prio().getJobOrder().equals(own), "Re-enabling override lost order");
        check(pico.getComponent(JailSolver.class).getSort().getJobOrder().equals(defaults), "TOP settings leaked to UWU priority");
        System.out.println("VERIFIED disabled override round-trip, parent fallback, re-enable and independent priority defaults");
    }

    private static void explicitInitialOverride() {
        OmegaUltimate top = pico.getComponent(OmegaUltimate.class);
        List<Job> own = List.copyOf(top.getPsPrio().getOwnJobOrder());
        List<Job> parent = new ArrayList<>(top.getGroupPrioJobSort().getJobOrder());
        Collections.reverse(parent);
        apply(Map.of("top.priority", names(parent), "top.ps.priority", names(own), "top.ps.priority_override", true));
        check(top.getPsPrio().getOwnJobOrder().equals(own), "Initial override enable discarded explicitly submitted equal order");
        check(top.getPsPrio().getJobOrder().equals(own), "Explicit initial override did not become effective");
        apply(Map.of("top.priority", names(own), "top.monitor.priority_override", true));
        check(top.getMonitorPrio().getJobOrder().equals(own), "Unset override stopped inheriting parent on first enable");
        System.out.println("VERIFIED first override enable retains explicit equal order and unset override inherits parent");
    }

    private static void maps() {
        OmegaUltimate top = pico.getComponent(OmegaUltimate.class);
        apply(Map.of("top.delta.markers", Map.of("NearWorld", Map.of("enabled", true, "marker", "TRIANGLE"),
                "DistantWorld", Map.of("enabled", false, "marker", "CROSS"))));
        check(top.getDeltaAmSettings().getMarkerFor(DynamisDeltaAssignment.NearWorld) == MarkerSign.TRIANGLE, "Configured sign did not reach native setting");
        check(top.getDeltaAmSettings().getMarkerFor(DynamisDeltaAssignment.DistantWorld) == null, "Disabled slot remained effective");
        check(top.getDeltaAmSettings().getSettings().get(DynamisDeltaAssignment.DistantWorld).getWhichMark().get() == MarkerSign.CROSS, "Disabled slot forgot chosen sign");
        JsonNode preset = setting("top.sigma.markers").path("presets").get(1).path("value");
        apply(Map.of("top.sigma.markers", preset));
        check(setting("top.sigma.markers").path("value").equals(preset), "Native preset did not round-trip");
        check(top.getSigmaAmSettings().getSettings().values().stream().allMatch(slot -> slot.getEnabled().get()), "Preset disabled native slots");
        apply(Map.of("top.delta.markers", setting("top.delta.markers").path("default")));
        check(top.getDeltaAmSettings().getMarkerFor(DynamisDeltaAssignment.NearWorld) == MarkerSign.IGNORE1, "Map reset changed default Near World");
        check(top.getDeltaAmSettings().getMarkerFor(DynamisDeltaAssignment.DistantWorld) == MarkerSign.IGNORE2, "Map reset changed default Distant World");
        check(!pico.getComponent(Dragonsong.class).getP6_useAutoMarks().get(), "Map edits enabled unrelated mechanic");
        System.out.println("VERIFIED exact native markers, retained disabled sign, Sausage preset and Defaults reset");
    }

    private static void delays() {
        JailSolver uwu = pico.getComponent(JailSolver.class);
        TelestoMain telesto = pico.getComponent(TelestoMain.class);
        apply(Map.of("uwu.clear_delay_ms", 5_000_000_000L, "top.sigma.delay_seconds", 50,
                "top.omega.first_delay_seconds", 28, "top.omega.second_delay_seconds", 20,
                "telesto.delay_base_ms", 5000, "telesto.delay_plus_ms", 0));
        check(uwu.getJailClearDelay().get() == 5_000_000_000L, "UWU long delay was truncated");
        check(telesto.getCommandDelayBase().get() == 5000 && telesto.getCommandDelayPlus().get() == 0, "Transport delays were not applied");
        apply(Map.of("uwu.clear_delay_ms", -1));
        check(uwu.getJailClearDelay().get() == -1, "Import invented bounds for native LongSetting");
        System.out.println("VERIFIED native timing boundaries, 64-bit UWU delay and transport controls");
    }

    private static void apply(Map<String, Object> values) {
        NativeAutomarkers.Plan plan = adapter.plan(tree(values));
        check(plan.changed(), "Expected a live setting change");
        plan.apply(Runnable::run);
    }

    private static JsonNode setting(String id) {
        for (JsonNode setting : tree(adapter.inventory()).path("settings")) {
            if (setting.path("id").asText().equals(id)) return setting;
        }
        throw new AssertionError("Missing setting " + id);
    }

    private static List<String> names(List<Job> jobs) { return jobs.stream().map(Job::name).toList(); }
    private static JsonNode tree(Object value) { return MAPPER.valueToTree(value); }
    private static void check(boolean passed, String message) { if (!passed) throw new AssertionError(message); }
}
