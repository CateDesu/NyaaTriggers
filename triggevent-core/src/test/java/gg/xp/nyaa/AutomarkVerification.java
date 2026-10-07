package gg.xp.nyaa;

import com.sun.net.httpserver.HttpServer;
import gg.xp.reevent.events.EventDistributor;
import gg.xp.reevent.events.EventMaster;
import gg.xp.telestosupport.*;
import gg.xp.xivdata.data.Job;
import gg.xp.xivsupport.events.actlines.events.AbilityUsedEvent;
import gg.xp.xivsupport.events.actlines.events.ZoneChangeEvent;
import gg.xp.xivsupport.events.state.PartyChangeEvent;
import gg.xp.xivsupport.events.state.PartyForceOrderChangeEvent;
import gg.xp.xivsupport.events.state.RawXivPartyInfo;
import gg.xp.xivsupport.events.state.XivStateImpl;
import gg.xp.xivsupport.events.triggers.duties.ewult.OmegaUltimate;
import gg.xp.xivsupport.events.triggers.duties.ewult.omega.PsMarkerAssignments;
import gg.xp.xivsupport.events.triggers.jails.FinalTitanJailsSolvedEvent;
import gg.xp.xivsupport.events.triggers.jails.JailSolver;
import gg.xp.xivsupport.events.triggers.marks.AutoMarkSlotRequest;
import gg.xp.xivsupport.events.triggers.marks.ClearAutoMarkRequest;
import gg.xp.xivsupport.events.triggers.marks.adv.AutoMarkServiceSelector;
import gg.xp.xivsupport.events.triggers.marks.adv.MarkerSign;
import gg.xp.xivsupport.events.triggers.marks.adv.SpecificAutoMarkRequest;
import gg.xp.xivsupport.models.*;
import gg.xp.xivsupport.models.groupmodels.PsMarkerGroup;
import gg.xp.xivsupport.replay.PullRecovery;
import org.picocontainer.MutablePicoContainer;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.BooleanSupplier;

public final class AutomarkVerification {
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final List<String> COMMANDS = new CopyOnWriteArrayList<>();
    private static final List<BaseTelestoResponse> RESPONSES = new CopyOnWriteArrayList<>();
    private static final List<SpecificAutoMarkRequest> ACTOR_MARKS = new CopyOnWriteArrayList<>();
    private static final ByteArrayOutputStream HOST_OUTPUT = new ByteArrayOutputStream();
    private static final AtomicInteger SELECTIONS = new AtomicInteger();
    private static final CountDownLatch FIRST = new CountDownLatch(1);
    private static final CountDownLatch RELEASE = new CountDownLatch(1);
    private static final CountDownLatch TIMEOUT_FIRST = new CountDownLatch(1);
    private static final CountDownLatch TIMEOUT_RELEASE = new CountDownLatch(1);
    private static volatile String mode = "good";
    private static volatile List<Map<String, String>> partyResponse = List.of();
    private static MutablePicoContainer pico;
    private static AutoMarkServiceSelector selector;
    private static EventMaster master;
    private static TelestoMain telesto;
    private static PullRecovery recovery;

    public static void main(String[] args) throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(Executors.newCachedThreadPool());
        server.createContext("/", exchange -> {
            JsonNode request = MAPPER.readTree(exchange.getRequestBody().readAllBytes());
            boolean command = request.path("type").asText().equals("ExecuteCommand");
            String text = request.path("payload").path("command").asText("");
            if (command) {
                COMMANDS.add(text);
                if (mode.equals("blocked") && text.contains("<1>")) {
                    FIRST.countDown();
                    try { RELEASE.await(15, TimeUnit.SECONDS); }
                    catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                }
                if (mode.equals("timeout") && text.contains("<6>")) {
                    TIMEOUT_FIRST.countDown();
                    try { TIMEOUT_RELEASE.await(10, TimeUnit.SECONDS); }
                    catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                }
            }
            int status = command && mode.equals("reject") ? 503 : 200;
            String body = command ? "null" : MAPPER.writeValueAsString(Map.of("id", request.path("id").asLong(), "response", partyResponse));
            if (command && mode.equals("malformed")) body = "not json";
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        try {
            var boot = TriggeventCore.class.getDeclaredMethod("bootEngine");
            boot.setAccessible(true);
            PrintStream terminal = System.out;
            try {
                System.setOut(new PrintStream(HOST_OUTPUT, true, StandardCharsets.UTF_8));
                pico = (MutablePicoContainer) boot.invoke(null);
            }
            finally { System.setOut(terminal); }
            master = pico.getComponent(EventMaster.class);
            telesto = pico.getComponent(TelestoMain.class);
            selector = pico.getComponent(AutoMarkServiceSelector.class);
            selector.addListener(SELECTIONS::incrementAndGet);
            var recoveryField = TriggeventCore.class.getDeclaredField("RECOVERY");
            recoveryField.setAccessible(true);
            recovery = (PullRecovery) recoveryField.get(null);
            pico.getComponent(EventDistributor.class).registerHandler(BaseTelestoResponse.class, (c, e) -> RESPONSES.add(e));
            pico.getComponent(EventDistributor.class).registerHandler(SpecificAutoMarkRequest.class, (c, e) -> ACTOR_MARKS.add(e));
            command(Map.of("nyaa_cmd", "set_automark", "enable", true,
                    "uri", "http://127.0.0.1:" + server.getAddress().getPort() + "/"));
            setDelay(0);
            await(() -> !RESPONSES.isEmpty(), "Initial party response did not finish");
            telesto.getEnablePartyList().set(false);
            if (args.length == 0 || args[0].equals("ordering")) ordering();
            if (args.length == 0 || args[0].equals("rejection")) rejection();
            if (args.length == 0) {
                malformed();
                timeout();
                configuration();
                supersededFailure();
                lifecycle();
                atomicControls();
                noOpControls();
                settingsLifetime();
                recoveredControls();
                configuredMap();
            }
            System.out.println("RESULT PASS");
            System.exit(0);
        }
        catch (Throwable error) {
            error.printStackTrace();
            String output = HOST_OUTPUT.toString(StandardCharsets.UTF_8);
            System.err.println(output.substring(Math.max(0, output.length() - 5000)));
            System.exit(1);
        }
        finally {
            RELEASE.countDown();
            TIMEOUT_RELEASE.countDown();
            server.stop(0);
        }
    }

    private static void ordering() throws Exception {
        mode = "blocked";
        COMMANDS.clear();
        mark(1);
        check(FIRST.await(5, TimeUnit.SECONDS), "First command did not reach the loopback endpoint");
        mark(2);
        Thread.sleep(250);
        check(COMMANDS.equals(List.of("/mk attack <1>")), "Second command overtook an unfinished first request: " + COMMANDS);
        RELEASE.countDown();
        await(() -> COMMANDS.size() == 2, "Ordered commands did not finish");
        check(COMMANDS.equals(List.of("/mk attack <1>", "/mk attack <2>")), "Native command order changed");
        mode = "good";
        System.out.println("VERIFIED serialized HTTP command requests and native marker order");
    }

    private static void rejection() throws Exception {
        mode = "reject";
        int errors = (int) RESPONSES.stream().filter(TelestoHttpError.class::isInstance).count();
        mark(3);
        await(() -> RESPONSES.stream().filter(TelestoHttpError.class::isInstance).count() > errors,
                "HTTP rejection had no response event");
        await(() -> telesto.getStatus() == TelestoStatus.BAD, "Rejected request appeared healthy");
        mode = "good";
        System.out.println("VERIFIED HTTP rejection reports bad transport status");
    }

    private static void malformed() throws Exception {
        mode = "malformed";
        int errors = (int) RESPONSES.stream().filter(TelestoConnectionError.class::isInstance).count();
        mark(4);
        await(() -> RESPONSES.stream().filter(TelestoConnectionError.class::isInstance).count() > errors,
                "Malformed success response had no failure event");
        check(telesto.getStatus() == TelestoStatus.BAD, "Malformed response appeared healthy");
        mode = "good";
        mark(5);
        await(() -> telesto.getStatus() == TelestoStatus.GOOD, "Null command response was not accepted");
        System.out.println("VERIFIED malformed response failure and protocol null response acceptance");
    }

    private static void lifecycle() throws Exception {
        setDelay(300);
        COMMANDS.clear();
        mark(6);
        command(Map.of("nyaa_cmd", "set_automark", "enable", false));
        command(Map.of("nyaa_cmd", "set_automark", "enable", true));
        mark(7);
        await(() -> !COMMANDS.isEmpty(), "Current mark did not follow cancelled output");
        Thread.sleep(400);
        check(COMMANDS.equals(List.of("/mk attack <7>")), "Disabled generation leaked queued marks: " + COMMANDS);
        command(Map.of("nyaa_cmd", "set_automark", "enable", false));
        mark(8);
        Thread.sleep(400);
        check(COMMANDS.size() == 1, "Disabled automarks reached the endpoint");
        System.out.println("VERIFIED pending generation cancellation and disabled negative case");
    }

    private static void timeout() throws Exception {
        mode = "timeout";
        COMMANDS.clear();
        long errors = RESPONSES.stream().filter(TelestoConnectionError.class::isInstance).count();
        mark(6);
        check(TIMEOUT_FIRST.await(5, TimeUnit.SECONDS), "Stalled command did not reach endpoint");
        mark(7);
        mark(8);
        await(() -> RESPONSES.stream().filter(TelestoConnectionError.class::isInstance).count() > errors,
                "Stalled command had no bounded failure");
        check(COMMANDS.equals(List.of("/mk attack <6>")), "Queued marks escaped after timeout: " + COMMANDS);
        mode = "good";
        TIMEOUT_RELEASE.countDown();
        mark(5);
        await(() -> COMMANDS.size() == 2, "Current mark did not resume after timeout");
        check(COMMANDS.equals(List.of("/mk attack <6>", "/mk attack <5>")), "Timeout retried or leaked stale actions");
        System.out.println("VERIFIED bounded timeout cancels stale burst without retries and current output resumes");
    }

    private static void configuration() throws Exception {
        command(Map.of("nyaa_cmd", "set_automark", "enable", true, "native_umad", false));
        setDelay(200);
        COMMANDS.clear();
        mark(1);
        command(Map.of("nyaa_cmd", "set_automark", "enable", true,
                "uri", telesto.getUriSetting().get().toString()));
        await(() -> COMMANDS.size() == 1, "Unchanged settings cancelled current output");
        check(COMMANDS.equals(List.of("/mk attack <1>")), "Unchanged settings changed marker output");
        COMMANDS.clear();
        mark(2);
        command(Map.of("nyaa_cmd", "set_automark",
                "uri", telesto.getUriSetting().get().resolve("changed").toString()));
        mark(3);
        await(() -> COMMANDS.size() == 1, "Changed endpoint did not accept current output");
        Thread.sleep(400);
        check(COMMANDS.equals(List.of("/mk attack <3>")), "Endpoint change leaked a queued old action: " + COMMANDS);
        var ownership = TriggeventCore.class.getDeclaredField("nativeUmadEnabled");
        ownership.setAccessible(true);
        check(!ownership.getBoolean(null), "URI control restored native UMAD ownership without requesting it");
        command(Map.of("nyaa_cmd", "set_automark", "enable", true, "native_umad", true));
        System.out.println("VERIFIED unchanged settings and partial controls preserve ownership while URI changes cancel old actions");
    }

    private static void atomicControls() throws Exception {
        mode = "good";
        command(Map.of("nyaa_cmd", "set_automark", "enable", true, "native_umad", false,
                "settings", Map.of("uwu.enabled", true, "top.sigma.delay_seconds", 7,
                        "telesto.delay_base_ms", 0, "telesto.delay_plus_ms", 0)));
        List<Map<String, Object>> invalid = new ArrayList<>(List.of(
                Map.of("enable", "false"), Map.of("enable", 1),
                Map.of("native_umad", "true"), Map.of("native_umad", 1),
                Map.of("uri", 1), Map.of("uri", "relative"), Map.of("uri", "ftp://127.0.0.1/"),
                Map.of("uri", "http://127.0.0.1:0/"), Map.of("uri", "http://127.0.0.1:70000/"),
                Map.of("settings", Map.of("uwu.enabled", false, "top.sigma.delay_seconds", 51)),
                Map.of("settings", Map.of("uwu.enabled", false, "unknown", true))));
        for (String key : List.of("enable", "native_umad", "uri", "settings")) {
            Map<String, Object> nil = new LinkedHashMap<>();
            nil.put(key, null);
            invalid.add(nil);
        }
        for (Map<String, Object> wrong : invalid) {
            Map<String, Object> changes = new LinkedHashMap<>(Map.of("nyaa_cmd", "set_automark", "enable", false,
                    "native_umad", true, "uri", telesto.getUriSetting().get().resolve("rejected").toString(),
                    "settings", Map.of("uwu.enabled", false, "top.sigma.delay_seconds", 8)));
            changes.putAll(wrong);
            ControlState before = controlState();
            BooleanSupplier permit = recovery.outputPermit();
            long diagnostics = records("engine_automark_config").size();
            check(diagnostics > 0, "Host config diagnostics were not captured");
            command(changes);
            check(controlState().equals(before), "Rejected controls changed live state or cached command: " + wrong);
            check(permit.getAsBoolean(), "Rejected controls cancelled pending output");
            check(records("engine_automark_config").size() == diagnostics, "Rejected controls emitted successful config diagnostics");
            List<JsonNode> inventory = records("automark_inventory");
            check(!inventory.isEmpty() && inventory.get(inventory.size() - 1).hasNonNull("error"), "Rejected controls had no visible error");
        }
        System.out.println("VERIFIED 15 invalid outer and nested controls leave flags, cache, URI, selector, settings and permits unchanged");
    }

    private static void noOpControls() throws Exception {
        command(Map.of("nyaa_cmd", "set_automark", "enable", false));
        ControlState before = controlState();
        BooleanSupplier permit = recovery.outputPermit();
        int selections = SELECTIONS.get();
        command(Map.of("nyaa_cmd", "set_automark", "enable", false));
        check(controlState().equals(before) && permit.getAsBoolean(), "Repeated disabled control retired marker state");
        check(SELECTIONS.get() == selections, "Repeated disabled control notified owners");
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("top.sigma.delay_seconds", 8)));
        check(SELECTIONS.get() == selections + 1, "Changed disabled settings did not notify owners exactly once");
        check(generation() == before.generation() + 1 && !permit.getAsBoolean(), "Changed disabled settings did not retire one output generation");
        check(selected().equals("none"), "Editing settings enabled markers");
        ControlState changed = controlState();
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("top.sigma.delay_seconds", 8)));
        check(controlState().equals(changed) && SELECTIONS.get() == selections + 1, "Identical settings replay reset disabled owners");
        System.out.println("VERIFIED disabled no-op controls preserve permits and changed disabled settings notify owners once");
    }

    private static void settingsLifetime() throws Exception {
        command(Map.of("nyaa_cmd", "set_automark", "enable", true,
                "settings", Map.of("telesto.delay_base_ms", 250, "telesto.delay_plus_ms", 0)));
        COMMANDS.clear();
        long accepted = acceptedCommands();
        mark(2);
        master.pushEventAndWait(new ClearAutoMarkRequest());
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("top.sigma.delay_seconds", 9)));
        mark(3);
        awaitCommands(List.of("/mk attack <3>"), accepted);

        List<XivPlayerCharacter> party = seedParty(0x309, 3);
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("uwu.enabled", true,
                "uwu.clear_delay_ms", 800, "telesto.delay_base_ms", 0, "telesto.delay_plus_ms", 0)));
        COMMANDS.clear();
        accepted = acceptedCommands();
        master.pushEventAndWait(new FinalTitanJailsSolvedEvent(party));
        awaitCommands(List.of("/mk attack <1>", "/mk attack <2>", "/mk attack <3>"), accepted);
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("top.sigma.delay_seconds", 10)));
        Thread.sleep(1100);
        check(COMMANDS.equals(List.of("/mk attack <1>", "/mk attack <2>", "/mk attack <3>")),
                "Old owned jail timer cleared markers after settings changed: " + COMMANDS);
        mark(3);
        awaitCommands(List.of("/mk attack <1>", "/mk attack <2>", "/mk attack <3>", "/mk attack <3>"), accepted);
        System.out.println("VERIFIED settings changes cancel queued marks and clears plus old owned jail timers while current output resumes");
    }

    private static void recoveredControls() throws Exception {
        command(Map.of("nyaa_cmd", "set_automark", "enable", true, "native_umad", true,
                "settings", Map.of("uwu.enabled", true, "top.sigma.delay_seconds", 11)));
        command(Map.of("nyaa_cmd", "recover_begin", "time", Instant.now().minusSeconds(2).toString()));
        command(Map.of("nyaa_cmd", "set_automark", "native_umad", false,
                "settings", Map.of("uwu.enabled", false, "top.sigma.delay_seconds", 12)));
        String uri = telesto.getUriSetting().get().resolve("recovered").toString();
        command(Map.of("nyaa_cmd", "set_automark", "uri", uri));
        ControlState valid = controlState();
        command(Map.of("nyaa_cmd", "set_automark", "enable", false, "native_umad", true,
                "uri", telesto.getUriSetting().get().resolve("invalid-recovery").toString(),
                "settings", Map.of("uwu.enabled", true, "top.sigma.delay_seconds", 51)));
        check(controlState().equals(valid), "Invalid recovery settings replaced the valid deferred command");
        check(!cached().path("native_umad").booleanValue() && cached().path("enable").booleanValue(), "Partial URI lost deferred boolean choices");
        check(cached().path("settings").path("uwu.enabled").isBoolean(), "Partial URI lost deferred settings");
        List<Boolean> enabledAfterSettings = new CopyOnWriteArrayList<>();
        selector.addListener(() -> {
            if (selected().equals("telesto-am")) {
                enabledAfterSettings.add(!pico.getComponent(JailSolver.class).getEnableAutomark().get()
                        && pico.getComponent(OmegaUltimate.class).getSigmaAmDelay().get() == 12 && !nativeUmad());
            }
        });
        COMMANDS.clear();
        command(Map.of("nyaa_cmd", "recover_end", "checkpoint", 1));
        check(enabledAfterSettings.equals(List.of(true)), "Recovery enabled output before applying settings and ownership");
        check(selected().equals("telesto-am") && telesto.getUriSetting().get().toString().equals(uri) && !nativeUmad(), "Recovery did not restore valid merged controls");
        check(COMMANDS.isEmpty(), "Recovery control application emitted historical markers");

        command(Map.of("nyaa_cmd", "recover_begin", "time", Instant.now().toString()));
        command(Map.of("nyaa_cmd", "set_automark", "enable", false,
                "settings", Map.of("top.sigma.delay_seconds", 13)));
        command(Map.of("nyaa_cmd", "set_automark", "uri", telesto.getUriSetting().get().resolve("disabled-recovery").toString()));
        command(Map.of("nyaa_cmd", "recover_end", "checkpoint", 2));
        check(selected().equals("none") && !requested() && !cached().path("enable").booleanValue(), "Partial URI revived a disabled recovery command");
        check(!nativeUmad() && pico.getComponent(OmegaUltimate.class).getSigmaAmDelay().get() == 13, "Disabled recovery lost settings or ownership");
        mark(4);
        Thread.sleep(100);
        check(COMMANDS.isEmpty(), "Disabled recovery emitted marker output");
        System.out.println("VERIFIED recovery merges partial controls, rejects invalid cache changes and applies settings before enabling output");
    }

    private static void configuredMap() throws Exception {
        List<XivPlayerCharacter> party = seedParty(0x462, 2);
        Map<String, Object> markers = new LinkedHashMap<>();
        pico.getComponent(OmegaUltimate.class).getPsMarkSettings().getSettings().forEach((slot, setting) ->
                markers.put(slot.name(), Map.of("enabled", false, "marker", setting.getWhichMark().get().name())));
        markers.put("GROUP1_CIRCLE", Map.of("enabled", true, "marker", "BIND1"));
        markers.put("GROUP2_CIRCLE", Map.of("enabled", true, "marker", "BIND2"));
        command(Map.of("nyaa_cmd", "set_automark", "enable", true, "settings", Map.of("top.ps.enabled", true,
                "top.ps.mid_markers", markers, "telesto.delay_base_ms", 0, "telesto.delay_plus_ms", 0)));
        await(() -> pico.getComponent(TelestoPartyListHandler.class).matchesSlot(party.get(0).getId(), 1)
                && pico.getComponent(TelestoPartyListHandler.class).matchesSlot(party.get(1).getId(), 2), "Authoritative party response was not applied");
        EnumMap<PsMarkerGroup, XivPlayerCharacter> assignments = new EnumMap<>(PsMarkerGroup.class);
        assignments.put(PsMarkerGroup.GROUP1_CIRCLE, party.get(1));
        assignments.put(PsMarkerGroup.GROUP2_CIRCLE, party.get(0));
        ACTOR_MARKS.clear();
        COMMANDS.clear();
        long accepted = acceptedCommands();
        master.pushEventAndWait(new PsMarkerAssignments(assignments, true));
        awaitCommands(List.of("/mk bind1 <2>", "/mk bind2 <1>"), accepted);
        check(ACTOR_MARKS.size() == 2 && ACTOR_MARKS.get(0).getMarker() == MarkerSign.BIND1
                && ACTOR_MARKS.get(0).getPlayerToMark().getId() == party.get(1).getId()
                && ACTOR_MARKS.get(1).getMarker() == MarkerSign.BIND2
                && ACTOR_MARKS.get(1).getPlayerToMark().getId() == party.get(0).getId(), "Configured map selected wrong actors or signs");
        master.pushEventAndWait(new AbilityUsedEvent(new XivAbility(0x7B30), new XivCombatant(0x40000001, "Omega"), party.get(0), List.of(), 1, 0, 1));
        List<String> expected = new ArrayList<>(List.of("/mk bind1 <2>", "/mk bind2 <1>"));
        for (int slot = 1; slot <= 8; slot++) expected.add("/mk clear <" + slot + ">");
        awaitCommands(expected, accepted);
        command(Map.of("nyaa_cmd", "set_automark", "settings", Map.of("top.ps.enabled", false)));
        master.pushEventAndWait(new PsMarkerAssignments(assignments, true));
        Thread.sleep(100);
        check(COMMANDS.equals(expected) && ACTOR_MARKS.size() == 2, "Disabled mechanic emitted stale or duplicate markers");
        System.out.println("VERIFIED configured native map marks exact actors BIND1/BIND2 in authoritative slots 2/1 then clears 1 through 8 without disabled duplicates");
    }

    private record ControlState(JsonNode command, boolean requested, boolean nativeUmad, String uri,
                                String service, JsonNode settings, long generation, int selections) {}

    private static ControlState controlState() throws Exception {
        NativeAutomarkers adapter = (NativeAutomarkers) field("NATIVE_AUTOMARKERS");
        return new ControlState(cached().deepCopy(), requested(), nativeUmad(), telesto.getUriSetting().get().toString(),
                selected(), MAPPER.valueToTree(adapter.inventory()), generation(), SELECTIONS.get());
    }

    private static JsonNode cached() throws Exception { return (JsonNode) field("automarkCommand"); }
    private static boolean requested() throws Exception { return (boolean) field("requestedAutomark"); }

    private static boolean nativeUmad() {
        try { return (boolean) field("nativeUmadEnabled"); }
        catch (ReflectiveOperationException error) { throw new AssertionError(error); }
    }

    private static Object field(String name) throws ReflectiveOperationException {
        var field = TriggeventCore.class.getDeclaredField(name);
        field.setAccessible(true);
        return field.get(null);
    }

    private static long generation() throws Exception {
        var field = PullRecovery.class.getDeclaredField("outputGeneration");
        field.setAccessible(true);
        return ((AtomicLong) field.get(recovery)).get();
    }

    private static String selected() { return selector.getEffectiveOption().descriptor().id(); }

    private static List<JsonNode> records(String type) {
        List<JsonNode> records = new ArrayList<>();
        String output = HOST_OUTPUT.toString(StandardCharsets.UTF_8);
        output = output.substring(0, output.lastIndexOf('\n') + 1);
        for (String line : output.split("\\R")) {
            if (!line.startsWith("{")) continue;
            JsonNode record = MAPPER.readTree(line);
            if (record.path("t").asText("").equals(type)
                    || record.path("t").asText("").equals("diagnostic") && record.path("event").asText("").equals(type)) records.add(record);
        }
        return records;
    }

    private static long acceptedCommands() {
        return RESPONSES.stream().filter(response -> response instanceof TelestoResponse accepted
                && accepted.getResponseTo() != null
                && accepted.getResponseTo().getJson().path("type").asText("").equals("ExecuteCommand")).count();
    }

    private static void awaitCommands(List<String> expected, long acceptedBefore) throws Exception {
        await(() -> COMMANDS.size() >= expected.size() && acceptedCommands() >= acceptedBefore + expected.size(),
                "Controlled command responses did not finish: " + COMMANDS);
        check(COMMANDS.equals(expected), "Unexpected marker commands: " + COMMANDS);
    }

    private static List<XivPlayerCharacter> seedParty(long zone, int size) throws Exception {
        master.pushEventAndWait(new ZoneChangeEvent(new XivZone(zone, "Fixture zone")));
        List<RawXivPartyInfo> raw = new ArrayList<>();
        List<Map<String, String>> response = new ArrayList<>();
        for (int index = 0; index < size; index++) {
            long id = 0x10000001L + index;
            raw.add(new RawXivPartyInfo(id, "Player" + index, 0, Job.WHM.getId(), 100, true));
            response.add(Map.of("actor", Long.toHexString(id), "order", Integer.toHexString(index + 1)));
        }
        partyResponse = List.copyOf(response);
        master.pushEventAndWait(new PartyChangeEvent(raw));
        master.pushEventAndWait(new PartyForceOrderChangeEvent(null));
        XivStateImpl state = pico.getComponent(XivStateImpl.class);
        state.setPlayer(new XivEntity(0x10000001L, "Player0"));
        check(state.getPartyList().size() == size, "Party fixture was not committed");
        if (telesto.getEnablePartyList().get()) {
            telesto.refreshPartyIfEnabled();
            await(() -> pico.getComponent(TelestoPartyListHandler.class).matchesSlot(0x10000001L, 1), "Party fixture had no authoritative response");
        }
        return List.copyOf(state.getPartyList());
    }

    private static void mark(int slot) { master.pushEventAndWait(new AutoMarkSlotRequest(slot)); }

    private static void supersededFailure() throws Exception {
        for (boolean endpointChanged : List.of(false, true)) {
            CountDownLatch failed = new CountDownLatch(1);
            CountDownLatch release = new CountDownLatch(1);
            telesto.setDeliveryFailure(() -> {
                BooleanSupplier permit = recovery.cancelPendingOutput();
                failed.countDown();
                try { release.await(5, TimeUnit.SECONDS); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                return permit;
            });
            try {
                mode = "reject";
                COMMANDS.clear();
                setDelay(0);
                long errors = RESPONSES.stream().filter(TelestoHttpError.class::isInstance).count();
                mark(1);
                check(failed.await(5, TimeUnit.SECONDS), "Rejected request did not cancel its generation");
                mode = "good";
                if (endpointChanged) {
                    command(Map.of("nyaa_cmd", "set_automark", "enable", true,
                            "uri", telesto.getUriSetting().get().resolve("replacement").toString()));
                    telesto.getEnablePartyList().set(false);
                }
                else {
                    command(Map.of("nyaa_cmd", "set_automark", "enable", false));
                    command(Map.of("nyaa_cmd", "set_automark", "enable", true));
                    telesto.getEnablePartyList().set(false);
                }
                setDelay(200);
                mark(2);
                release.countDown();
                await(() -> RESPONSES.stream().filter(TelestoHttpError.class::isInstance).count() > errors,
                        "Superseded failure event was not processed");
                await(() -> COMMANDS.size() == 2, "Old failure cancelled a new marker");
                check(COMMANDS.equals(List.of("/mk attack <1>", "/mk attack <2>")),
                        "Superseded failure changed current marker output: " + COMMANDS);
                BaseTelestoResponse error = RESPONSES.stream().filter(TelestoHttpError.class::isInstance)
                        .reduce((first, second) -> second).orElseThrow();
                check(!error.isCurrent(), "Superseded failure still owns current marker state");
                await(() -> telesto.getStatus() == TelestoStatus.GOOD, "Current marker did not restore healthy status");
            }
            finally {
                release.countDown();
                telesto.setDeliveryFailure(recovery::cancelPendingOutput);
            }
        }
        System.out.println("VERIFIED superseded endpoint and disabled generations cannot cancel current markers");
    }

    private static void command(Map<String, ?> command) throws Exception {
        var method = TriggeventCore.class.getDeclaredMethod("handleCommand", JsonNode.class);
        method.setAccessible(true);
        method.invoke(null, MAPPER.valueToTree(command));
    }

    private static void setDelay(int delay) {
        for (var setting : List.of(telesto.getCommandDelayBase(), telesto.getCommandDelayPlus())) {
            try { setting.set(delay); }
            catch (RuntimeException ignored) {}
        }
    }

    private static void await(BooleanSupplier condition, String reason) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(6);
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) Thread.sleep(20);
        check(condition.getAsBoolean(), reason);
    }

    private static void check(boolean condition, String reason) {
        if (!condition) throw new AssertionError(reason);
    }
}
