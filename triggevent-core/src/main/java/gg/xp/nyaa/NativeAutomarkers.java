package gg.xp.nyaa;

import gg.xp.telestosupport.TelestoMain;
import gg.xp.xivdata.data.Job;
import gg.xp.xivsupport.events.triggers.duties.Dragonsong;
import gg.xp.xivsupport.events.triggers.duties.ewult.OmegaUltimate;
import gg.xp.xivsupport.events.triggers.jails.JailSolver;
import gg.xp.xivsupport.events.triggers.marks.adv.MarkerSign;
import gg.xp.xivsupport.gui.util.HasFriendlyName;
import gg.xp.xivsupport.persistence.settings.*;
import org.picocontainer.PicoContainer;
import tools.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.LongConsumer;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

final class NativeAutomarkers {
    private final Map<String, Binding> bindings = new LinkedHashMap<>();
    private final List<Map<String, Object>> mechanics = new ArrayList<>();
    private final Map<String, JobSortSetting> priorities = new LinkedHashMap<>();

    NativeAutomarkers(PicoContainer pico) {
        JailSolver uwu = pico.getComponent(JailSolver.class);
        Dragonsong dsr = pico.getComponent(Dragonsong.class);
        OmegaUltimate top = pico.getComponent(OmegaUltimate.class);
        TelestoMain telesto = pico.getComponent(TelestoMain.class);

        bool("uwu.enabled", "Enable Titan Gaols markers", uwu.getEnableAutomark(), true);
        bool("uwu.override_zone_lock", "Allow outside UWU", uwu.getOverrideZoneLock(), false);
        jobs("uwu.priority", "Gaols priority", uwu.getSort(), null, null);
        number("uwu.clear_delay_ms", "Clear delay milliseconds", uwu.getJailClearDelay()::get,
                uwu.getJailClearDelay()::set, 10000, null, null);

        bool("dsr.p5.enabled", "Enable Thunderstruck markers", dsr.getP5_thunderstruckAutoMarks(), false);
        bool("dsr.p6.enabled", "Enable Wroth Flames markers", dsr.getP6_useAutoMarks(), false);
        bool("dsr.p6.rot_priority", "Prioritize Rot holders", dsr.getP6_rotPrioHigh(), true);
        bool("dsr.p6.reverse_priority", "Reverse priority", dsr.getP6_reverseSort(), true);
        jobs("dsr.p6.priority", "Wroth priority", dsr.getP6_sortSetting(), null, null);
        markers("dsr.p6.markers", "Wroth markers", dsr.getP6_amAssignments());

        jobs("top.priority", "Shared TOP priority", top.getGroupPrioJobSort(), null, null);
        bool("top.looper.enabled", "Enable Program Loop markers", top.getLooperAM(), false);
        bool("top.panto.enabled", "Enable Pantokrator markers", top.getPantoAmEnable(), false);
        bool("top.ps.enabled", "Enable Party Synergy markers", top.getPsAmEnable(), false);
        bool("top.sniper.enabled", "Enable Sniper Cannon markers", top.getSniperAmEnable(), false);
        bool("top.monitor.enabled", "Enable monitor markers", top.getMonitorAmEnable(), false);
        bool("top.delta.enabled", "Enable Delta markers", top.getDeltaAmEnable(), false);
        bool("top.sigma.enabled", "Enable Sigma markers", top.getSigmaAmEnable(), false);
        bool("top.omega.enabled", "Enable Omega markers", top.getOmegaAmEnable(), false);
        bool("top.omega.first_enabled", "Mark first Omega set", top.getOmegaAmFirstSetEnable(), true);
        bool("top.omega.second_enabled", "Mark second Omega set", top.getOmegaAmSecondSetEnable(), true);
        override("top.p1", "P1 priority", top.getP1prio());
        override("top.ps", "Party Synergy priority", top.getPsPrio());
        override("top.sniper", "Sniper priority", top.getSniperPrio());
        override("top.monitor", "Monitor priority", top.getMonitorPrio());
        override("top.sigma", "Sigma priority", top.getSigmaPsPrio());
        override("top.omega", "Omega priority", top.getOmegaPsPrio());
        markers("top.p1.markers", "Line markers", top.getMarkSettings());
        markers("top.ps.mid_markers", "Mid glitch markers", top.getPsMarkSettings());
        markers("top.ps.far_markers", "Far glitch markers", top.getPsMarkSettingsFarGlitch());
        markers("top.sniper.markers", "Sniper markers", top.getSniperAmSettings());
        markers("top.delta.markers", "Delta markers", top.getDeltaAmSettings());
        markers("top.sigma.markers", "Sigma markers", top.getSigmaAmSettings());
        markers("top.omega.markers", "Omega markers", top.getOmegaAmSettings());
        integer("top.sigma.delay_seconds", "Sigma delay seconds", top.getSigmaAmDelay(), 0);
        integer("top.omega.first_delay_seconds", "First Omega delay seconds", top.getOmegaFirstSetDelay(), 1);
        integer("top.omega.second_delay_seconds", "Second Omega delay seconds", top.getOmegaSecondSetDelay(), 0);
        integer("telesto.delay_base_ms", "Command delay milliseconds", telesto.getCommandDelayBase(), 100);
        integer("telesto.delay_plus_ms", "Additional random delay milliseconds", telesto.getCommandDelayPlus(), 100);

        mechanic("uwu.gaols", "UWU", "Titan Gaols", "uwu.enabled", "uwu.priority", "uwu.clear_delay_ms", "uwu.override_zone_lock");
        mechanic("dsr.p5", "DSR", "P5 Thunderstruck", "dsr.p5.enabled");
        mechanic("dsr.p6", "DSR", "P6 Wroth Flames", "dsr.p6.enabled", "dsr.p6.priority", "dsr.p6.rot_priority", "dsr.p6.reverse_priority", "dsr.p6.markers");
        mechanic("top.looper", "TOP", "P1 Program Loop", "top.looper.enabled", "top.priority", "top.p1.priority_override", "top.p1.priority", "top.p1.markers");
        mechanic("top.panto", "TOP", "P1 Pantokrator", "top.panto.enabled", "top.priority", "top.p1.priority_override", "top.p1.priority", "top.p1.markers");
        mechanic("top.ps", "TOP", "P2 Party Synergy", "top.ps.enabled", "top.priority", "top.ps.priority_override", "top.ps.priority", "top.ps.mid_markers", "top.ps.far_markers");
        mechanic("top.sniper", "TOP", "P3 Sniper Cannon", "top.sniper.enabled", "top.priority", "top.sniper.priority_override", "top.sniper.priority", "top.sniper.markers");
        mechanic("top.monitor", "TOP", "P3 Monitors", "top.monitor.enabled", "top.priority", "top.monitor.priority_override", "top.monitor.priority");
        mechanic("top.delta", "TOP", "P5 Run Dynamis Delta", "top.delta.enabled", "top.delta.markers");
        mechanic("top.sigma", "TOP", "P5 Run Dynamis Sigma", "top.sigma.enabled", "top.priority", "top.sigma.priority_override", "top.sigma.priority", "top.sigma.markers", "top.sigma.delay_seconds");
        mechanic("top.omega.first", "TOP", "P5 Run Dynamis Omega first set", "top.omega.enabled", "top.omega.first_enabled", "top.priority", "top.omega.priority_override", "top.omega.priority", "top.omega.markers", "top.omega.first_delay_seconds");
        mechanic("top.omega.second", "TOP", "P5 Run Dynamis Omega second set", "top.omega.enabled", "top.omega.second_enabled", "top.priority", "top.omega.priority_override", "top.omega.priority", "top.omega.markers", "top.omega.second_delay_seconds");
        mechanic("umad.p4", "UMAD", "P4 Kefka and Grand Cross debuffs", "native_umad");
    }

    Map<String, Object> inventory() {
        List<Map<String, Object>> settings = new ArrayList<>();
        bindings.forEach((id, binding) -> {
            Map<String, Object> descriptor = binding.descriptor();
            if (priorities.containsKey(id)) descriptor.put("effective_order", jobNames(priorities.get(id).getJobOrder()));
            settings.add(descriptor);
        });
        settings.add(Map.of("id", "native_umad", "type", "boolean", "label", "Enable native P4 debuff markers",
                "managed", "frontend", "default", true));
        return Map.of("t", "automark_inventory", "version", 1, "mechanics", List.copyOf(mechanics), "settings", settings,
                "jobs", Arrays.stream(Job.values()).filter(Job::isCombatJob)
                        .map(job -> Map.of("id", job.name(), "label", job.getFriendlyName())).toList(),
                "markers", Arrays.stream(MarkerSign.values())
                        .map(marker -> Map.of("id", marker.name(), "label", marker.getFriendlyName())).toList());
    }

    Plan plan(JsonNode settings) {
        if (settings == null || settings.isMissingNode()) return new Plan(List.of());
        require(settings.isObject(), "Automarker settings must be an object");
        Map<String, Object> submitted = new LinkedHashMap<>();
        settings.properties().forEach(entry -> {
            Binding binding = bindings.get(entry.getKey());
            require(binding != null, "Unknown automarker setting: " + entry.getKey());
            submitted.put(entry.getKey(), binding.parse.apply(entry.getValue()));
        });
        List<Change> changes = new ArrayList<>();
        submitted.forEach((id, value) -> {
            Binding binding = bindings.get(id);
            String enabled = (String) binding.metadata.get("override_enabled");
            boolean firstEnable = enabled != null && Boolean.TRUE.equals(submitted.get(enabled))
                    && Boolean.FALSE.equals(bindings.get(enabled).read.get());
            if (firstEnable || !value.equals(binding.read.get())) changes.add(new Change(binding, value));
        });
        changes.sort(Comparator.comparingInt(change -> change.binding.order));
        return new Plan(List.copyOf(changes));
    }

    static final class Plan {
        private final List<Change> changes;

        private Plan(List<Change> changes) { this.changes = changes; }

        boolean changed() { return !changes.isEmpty(); }

        void apply(Consumer<Runnable> write) {
            changes.forEach(change -> change.binding.write.accept(change.value, write));
        }
    }

    private record Change(Binding binding, Object value) {}

    @FunctionalInterface
    private interface Writer { void accept(Object value, Consumer<Runnable> write); }

    private record Binding(Map<String, Object> metadata, Supplier<Object> read, Supplier<Object> defaults,
                           Function<JsonNode, Object> parse, Writer write, int order) {
        Map<String, Object> descriptor() {
            Map<String, Object> result = new LinkedHashMap<>(metadata);
            result.put("value", read.get());
            result.put("default", defaults.get());
            return result;
        }
    }

    private void bool(String id, String label, BooleanSetting setting, boolean dflt) {
        add(id, new Binding(metadata(id, "boolean", label), setting::get, () -> dflt,
                node -> { require(node.isBoolean(), id + " must be a boolean"); return node.booleanValue(); },
                (value, write) -> write.accept(() -> setting.set((boolean) value)), 2));
    }

    private void integer(String id, String label, IntSetting setting, int dflt) {
        number(id, label, () -> setting.get(), value -> setting.set(Math.toIntExact(value)), dflt,
                setting.getMin() == null ? null : setting.getMin().longValue(),
                setting.getMax() == null ? null : setting.getMax().longValue());
    }

    private void number(String id, String label, LongSupplier read, LongConsumer write, long dflt, Long min, Long max) {
        Map<String, Object> meta = metadata(id, "integer", label);
        if (min != null) meta.put("min", min);
        if (max != null) meta.put("max", max);
        add(id, new Binding(meta, read::getAsLong, () -> dflt, node -> {
            require(node.isIntegralNumber() && node.canConvertToLong(), id + " must be an integer");
            long value = node.longValue();
            require(min == null || value >= min, id + " is below its minimum");
            require(max == null || value <= max, id + " is above its maximum");
            return value;
        }, (value, setter) -> setter.accept(() -> write.accept((long) value)), 1));
    }

    private void override(String prefix, String label, JobSortOverrideSetting setting) {
        bool(prefix + ".priority_override", "Override shared priority", setting.getEnabled(), false);
        jobs(prefix + ".priority", label, setting, "top.priority", prefix + ".priority_override");
    }

    private void jobs(String id, String label, JobSortSetting setting, String parent, String enabled) {
        Map<String, Object> meta = metadata(id, "jobs", label);
        if (parent != null) {
            meta.put("parent", parent);
            meta.put("override_enabled", enabled);
        }
        Supplier<List<Job>> own = setting instanceof JobSortOverrideSetting override
                ? override::getOwnJobOrder : setting::getJobOrder;
        Binding binding = new Binding(meta, () -> jobNames(own.get()), () -> jobNames(setting.getDefaultJobOrder()), node -> {
            require(node.isArray(), id + " must be a job order");
            List<Job> jobs = new ArrayList<>();
            node.forEach(value -> {
                require(value.isTextual(), id + " contains a non-text job");
                try { jobs.add(Job.valueOf(value.textValue())); }
                catch (IllegalArgumentException error) { throw new IllegalArgumentException(id + " contains an unknown job"); }
            });
            try { setting.validateJobSortOrder(jobs); }
            catch (JobSortValidationException error) { throw new IllegalArgumentException(id + " must contain every combat job exactly once", error); }
            return jobNames(jobs);
        }, (value, write) -> {
            @SuppressWarnings("unchecked") List<String> names = (List<String>) value;
            write.accept(() -> setting.setJobOrder(names.stream().map(Job::valueOf).toList()));
        }, 0);
        add(id, binding);
        priorities.put(id, setting);
    }

    private <E extends Enum<E>> void markers(String id, String label, MultiSlotAutomarkSetting<E> setting) {
        Map<String, Object> meta = metadata(id, "marker_map", label);
        meta.put("slots", setting.getSettings().keySet().stream()
                .map(slot -> Map.of("id", slot.name(), "label", slot instanceof HasFriendlyName friendly ? friendly.getFriendlyName() : slot.name())).toList());
        meta.put("presets", setting.getPresets().stream().map(preset -> Map.of("name", preset.getName(),
                "value", presetMap(setting, preset))).toList());
        add(id, new Binding(meta, () -> markerMap(setting), () -> presetMap(setting, setting.getPresets().get(0)), node -> {
            require(node.isObject(), id + " must be a marker map");
            Set<String> expected = setting.getSettings().keySet().stream().map(Enum::name).collect(java.util.stream.Collectors.toSet());
            require(node.propertyNames().equals(expected), id + " must contain every known slot exactly once");
            Map<String, Object> result = new LinkedHashMap<>();
            setting.getSettings().keySet().forEach(slot -> {
                JsonNode value = node.path(slot.name());
                require(value.isObject() && value.propertyNames().equals(Set.of("enabled", "marker")), id + " has an invalid slot value");
                require(value.path("enabled").isBoolean() && value.path("marker").isTextual(), id + " has invalid slot types");
                MarkerSign marker;
                try { marker = MarkerSign.valueOf(value.path("marker").textValue()); }
                catch (IllegalArgumentException error) { throw new IllegalArgumentException(id + " has an unknown marker"); }
                result.put(slot.name(), Map.of("enabled", value.path("enabled").booleanValue(), "marker", marker.name()));
            });
            return result;
        }, (value, write) -> {
            @SuppressWarnings("unchecked") Map<String, Map<String, Object>> choices = (Map<String, Map<String, Object>>) value;
            setting.getSettings().forEach((slot, control) -> {
                Map<String, Object> choice = choices.get(slot.name());
                MarkerSign marker = MarkerSign.valueOf((String) choice.get("marker"));
                boolean enable = (boolean) choice.get("enabled");
                if (control.getWhichMark().get() != marker) write.accept(() -> control.getWhichMark().set(marker));
                if (control.getEnabled().get() != enable) write.accept(() -> control.getEnabled().set(enable));
            });
        }, 1));
    }

    private static <E extends Enum<E>> Map<String, Object> markerMap(MultiSlotAutomarkSetting<E> setting) {
        Map<String, Object> result = new LinkedHashMap<>();
        setting.getSettings().forEach((slot, control) -> result.put(slot.name(),
                Map.of("enabled", control.getEnabled().get(), "marker", control.getWhichMark().get().name())));
        return result;
    }

    private static <E extends Enum<E>> Map<String, Object> presetMap(MultiSlotAutomarkSetting<E> setting, MultiSlotAutomarkPreset<E> preset) {
        Map<String, Object> result = new LinkedHashMap<>();
        setting.getSettings().forEach((slot, control) -> {
            MarkerSign marker = preset.getPresetData().get(slot);
            result.put(slot.name(), Map.of("enabled", marker != null, "marker",
                    marker == null ? control.getWhichMark().getDefault().name() : marker.name()));
        });
        return result;
    }

    private static List<String> jobNames(List<Job> jobs) { return jobs.stream().map(Job::name).toList(); }

    private static Map<String, Object> metadata(String id, String type, String label) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", id);
        result.put("type", type);
        result.put("label", label);
        return result;
    }

    private void add(String id, Binding binding) {
        if (bindings.putIfAbsent(id, binding) != null) throw new IllegalArgumentException("Duplicate setting " + id);
    }

    private void mechanic(String id, String fight, String name, String... settings) {
        List<String> refs = new ArrayList<>(List.of(settings));
        refs.add("telesto.delay_base_ms");
        refs.add("telesto.delay_plus_ms");
        mechanics.add(Map.of("id", id, "fight", fight, "name", name, "settings", List.copyOf(refs)));
    }

    private static void require(boolean valid, String message) {
        if (!valid) throw new IllegalArgumentException(message);
    }
}
