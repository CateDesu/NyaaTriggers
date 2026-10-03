import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtQml import QJSEngine

from nyaatriggers.cactbot_reader import CactbotReader, _HARVEST_JS


APP = QCoreApplication.instance() or QCoreApplication([])


class CactbotRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = QJSEngine()
        self.js("""
            var window = this, reports = [], pending = [];
            var document = {getElementById: function() {return {};}};
            var MutationObserver = function() {this.observe = function() {};};
            var qt = {webChannelTransport: {}};
            var QWebChannel = function(transport, callback) {
                callback({objects: {harvester: {fromCactbot: function(kind, payload) {
                    reports.push([kind, JSON.parse(payload)]);
                }}}});
            };
            var setTimeout = function(callback) {pending.push(callback);};
            var WebSocket = function() {this.listeners = []; this.sent = [];};
            WebSocket.prototype.send = function(data) {this.sent.push(data);};
            WebSocket.prototype.addEventListener = function(kind, callback, capture) {
                this.listeners.push({kind: kind, callback: callback, capture: !!capture});
            };
            WebSocket.prototype.removeEventListener = function(kind, callback, capture) {
                this.listeners = this.listeners.filter(function(l) {
                    return l.kind !== kind || l.callback !== callback || l.capture !== !!capture;
                });
            };
            WebSocket.prototype.dispatch = function(event) {
                var socket = this;
                this.listeners.slice().forEach(function(l) {
                    if (l.kind === 'message') l.callback.call(socket, event);
                });
            };
            var privateOptions = {DisabledTriggers: {Existing: true}, Triggers: [{
                id: 'User set', zoneId: 400, triggers: [{id: 'User trigger'}],
                timelineTriggers: [{id: 'User timeline'}]
            }]};
            var manifest = {'builtin.ts': {
                id: 'Builtin set', zoneId: 500, triggers: [{id: 'Builtin trigger'}]
            }};
            var webpackChunkcactbot = [[[12], {77: function() {}}]];
            webpackChunkcactbot.push = function(chunk) {
                if (chunk[2]) chunk[2](function() {return {default: manifest};});
            };
            window.__nyaaDisabledTriggers = {'User trigger': true};
        """)
        self.js(_HARVEST_JS)

    def js(self, script):
        result = self.engine.evaluate(script)
        if result.isError():
            self.fail(result.toString())
        return result

    def load_user(self):
        self.js("""
            var socket = new WebSocket(), response;
            socket.addEventListener('message', function(event) {
                response = JSON.parse(event.data);
            });
            socket.send(JSON.stringify({call: 'cactbotLoadUser', overlayName: 'raidboss', rseq: 0}));
            socket.dispatch({data: JSON.stringify({rseq: 0, detail: {
                localUserFiles: {'raidboss.js': 'Options.DisabledTriggers.Existing = true;'}
            }})});
            (function() {
                var Options = privateOptions;
                Object.keys(response.detail.localUserFiles).sort().forEach(function(key) {
                    eval(response.detail.localUserFiles[key]);
                });
            })();
            var liveOptions = Object.assign({}, privateOptions);
            pending.splice(0).forEach(function(callback) {callback();});
        """)

    def test_saved_disables_reach_module_options_and_inventory_lists_trigger_ids(self):
        self.load_user()
        self.assertTrue(self.js("liveOptions.DisabledTriggers['User trigger'] === true").toBool())
        self.assertTrue(self.js("typeof window.Options === 'undefined'").toBool())
        inventory = json.loads(self.js("JSON.stringify(reports.filter(function(r) {return r[0] === 'triggers';}))").toString())
        self.assertTrue(inventory)
        self.assertEqual({row['id'] for row in inventory[-1][1]},
                         {'Builtin trigger', 'User trigger', 'User timeline'})

    def test_live_toggles_preserve_shared_map_and_existing_user_disables(self):
        self.load_user()
        reader = CactbotReader()
        reader._active = True

        class Page:
            def runJavaScript(page, script, _world):
                self.js(script)

        reader._page = Page()
        reader.set_disabled_triggers(['Builtin trigger', 'Existing'])
        self.assertTrue(self.js("liveOptions.DisabledTriggers === privateOptions.DisabledTriggers").toBool())
        self.assertTrue(self.js("liveOptions.DisabledTriggers['Builtin trigger'] === true").toBool())
        self.assertFalse(self.js("'User trigger' in liveOptions.DisabledTriggers").toBool())
        reader.set_disabled_triggers([])
        self.assertEqual(json.loads(self.js("JSON.stringify(liveOptions.DisabledTriggers)").toString()),
                         {'Existing': True})

    def test_unrelated_responses_and_sockets_keep_their_original_data(self):
        self.load_user()
        self.js("""
            var unrelated = {rseq: 8, detail: {localUserFiles: {}}};
            socket.dispatch({data: JSON.stringify(unrelated)});
        """)
        self.assertEqual(self.js("JSON.stringify(response)").toString(),
                         self.js("JSON.stringify(unrelated)").toString())
        self.js("""
            var other = new WebSocket(), untouched;
            other.addEventListener('message', function(event) {untouched = event.data;});
            other.send(JSON.stringify({call: 'getCombatants', rseq: 0}));
            other.dispatch({data: 'original data'});
        """)
        self.assertEqual(self.js("untouched").toString(), 'original data')

    def test_later_user_file_keeps_its_disable_when_the_program_reenables_it(self):
        self.js("""
            window.__nyaaDisabledTriggers = {Later: true};
            window.__nyaaCaptureOptions(privateOptions);
            privateOptions.DisabledTriggers.Later = true;
            pending.splice(0).forEach(function(callback) {callback();});
            window.__nyaaSetDisabledTriggers({});
        """)
        self.assertTrue(self.js("privateOptions.DisabledTriggers.Later === true").toBool())

    def test_feed_without_user_files_still_captures_raidboss_options(self):
        self.js("""
            var socket = new WebSocket(), response;
            socket.addEventListener('message', function(event) {response = JSON.parse(event.data);});
            socket.send(JSON.stringify({call: 'cactbotLoadUser', overlayName: 'raidboss', rseq: 0}));
            socket.dispatch({data: JSON.stringify({rseq: 0, detail: {localUserFiles: null}})});
        """)
        self.assertTrue(self.js("!!response.detail.localUserFiles").toBool())
        self.js("""
            (function() {
                var Options = privateOptions;
                Object.keys(response.detail.localUserFiles).forEach(function(key) {
                    eval(response.detail.localUserFiles[key]);
                });
            })();
            pending.splice(0).forEach(function(callback) {callback();});
        """)
        self.assertTrue(self.js("privateOptions.DisabledTriggers['User trigger'] === true").toBool())

    def test_removing_function_and_object_listeners_preserves_websocket_behavior(self):
        self.js("""
            var socket = new WebSocket(), count = 0;
            var listener = function() {count++;};
            var objectListener = {handleEvent: function() {count++;}};
            socket.addEventListener('message', listener);
            socket.addEventListener('message', objectListener, true);
            socket.dispatch({data: '{}'});
            socket.removeEventListener('message', listener);
            socket.removeEventListener('message', objectListener, true);
            socket.dispatch({data: '{}'});
        """)
        self.assertEqual(self.js("count").toInt(), 2)

    def test_reply_sequence_strings_match_numeric_requests(self):
        self.js("""
            var socket = new WebSocket(), response;
            socket.addEventListener('message', function(event) {response = JSON.parse(event.data);});
            socket.send(JSON.stringify({call: 'cactbotLoadUser', overlayName: 'raidboss', rseq: 0}));
            socket.dispatch({data: JSON.stringify({rseq: '0', detail: {localUserFiles: {}}})});
        """)
        self.assertTrue(self.js("Object.keys(response.detail.localUserFiles).length > 0").toBool())

    def test_global_options_and_overridden_sets_remain_supported(self):
        self.js("""
            window.Options = privateOptions;
            privateOptions.Triggers.push({id: 'Builtin set', zoneId: 500,
                triggers: [{id: 'Replacement trigger'}]});
            window.__raidbossLoadedTriggers = [{id: 'Flat trigger', zoneName: 'Flat zone'}];
            pending.splice(0).forEach(function(callback) {callback();});
        """)
        self.assertTrue(self.js("privateOptions.DisabledTriggers['User trigger'] === true").toBool())
        inventory = json.loads(self.js("JSON.stringify(reports.filter(function(r) {return r[0] === 'triggers';}))").toString())
        self.assertEqual({row['id'] for row in inventory[-1][1]},
                         {'User trigger', 'User timeline', 'Replacement trigger', 'Flat trigger'})

    def test_options_loaded_after_polling_expires_still_apply_saved_disables(self):
        self.js("""
            for (var i = 0; i < 50; i++) pending.splice(0).forEach(function(callback) {callback();});
        """)
        self.load_user()
        self.assertTrue(self.js("liveOptions.DisabledTriggers['User trigger'] === true").toBool())


if __name__ == '__main__':
    unittest.main()
