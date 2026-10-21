from copy import deepcopy
from collections import Counter
from pathlib import Path
import json
import os
import re
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET

from nyaatriggers.convert_triggernometry import MAX_XML_BYTES
from nyaatriggers.locale_util import _


_SCRIPT_GUID_RE = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f])")
_IDENTITY_FILE = ".nyaatriggers-identities.json"
_IDENTITY_LOCK = threading.RLock()


class _IdentityHistoryError(ValueError):
    pass


def normalized_id(value):
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return ""


def pack_backup_path(path):
    path = Path(path)
    backup = path.with_suffix(".xml.bak")
    return path.with_suffix(".bak") if len(os.fsencode(backup.name)) > 255 else backup


def matching_import(packs, source, raw):
    def identity(content):
        root = parse_xml(content)
        folder = root.find("ExportedFolder")
        if folder is None:
            raise ValueError(_("The file is not a Triggernometry folder export"))
        return normalized_id(folder.get("Id"))

    folder_id = identity(raw)
    matches = []
    for path in sorted(Path(packs).iterdir()):
        if not path.is_file() or path.suffix.lower() != ".xml":
            continue
        try:
            with path.open("rb") as stream:
                previous = stream.read(MAX_XML_BYTES + 1)
            other_folder = identity(previous)
        except (OSError, ValueError, RecursionError):
            continue
        if (path.resolve() == Path(source).resolve() or previous == raw
                or (folder_id and folder_id == other_folder)):
            matches.append((path, previous))
    if len(matches) > 1:
        conflicts = [path for path, _raw in matches]
        raise ValueError(_(
            "Multiple imported copies match this export:\n{paths}\n\n"
            "Choose one pack to replace before importing this version.").format(
                paths="\n".join(str(path) for path in conflicts)))
    return matches[0] if matches else None


def parse_xml(raw, tag="TriggernometryExport"):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_XML_BYTES:
        raise ValueError(_("The XML file exceeds the 16 MiB import limit"))
    scan = raw.replace(b"\x00", b"")
    if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
        raise ValueError(_("DOCTYPE and ENTITY declarations are not supported"))
    try:
        root = ET.fromstring(raw, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    except (ET.ParseError, LookupError) as exc:
        raise ValueError(str(exc)) from exc
    if root.tag != tag:
        raise ValueError(_("Unexpected XML element: {tag}").format(tag=root.tag))
    pending = [(root, 0)]
    count = 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if depth > 64 or count > 50000:
            raise ValueError(_("This XML is too deeply nested or contains too many elements"))
        pending.extend((child, depth + 1) for child in element)
    return root


def xml_bytes(root):
    raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    parse_xml(raw, root.tag)
    return raw


def _trigger_locations(root):
    entries = []

    def walk(folder, ancestry, location):
        ancestry = (*ancestry, folder)
        triggers = folder.findall("Triggers/Trigger")
        sources = [normalized_id(trigger.get("NyaaSourceId")) or normalized_id(trigger.get("Id"))
                   for trigger in triggers]
        counts = Counter(sources)
        occurrences = {}
        for trigger, source in zip(triggers, sources):
            key = source, trigger.get("Name", "") if counts[source] > 1 else ""
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            entries.append((trigger, ancestry, (location, *key, occurrence)))
        folders = folder.findall("Folders/Folder")
        ids = [normalized_id(child.get("Id")) for child in folders]
        counts = Counter(ids)
        occurrences = {}
        for child, ident in zip(folders, ids):
            key = (ident, child.get("Name", "") if not ident or counts[ident] > 1 else "")
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            walk(child, ancestry, (*location, (*key, occurrence)))

    for folder in root.findall("ExportedFolder"):
        walk(folder, (), ())
    return entries


def script_trigger_references(root, trigger_ids):
    ids = {ident for value in trigger_ids if (ident := normalized_id(value))}
    if not ids:
        return
    for _path, trigger, _parent in trigger_entries(root):
        for action in trigger.iter("Action"):
            if action.get("ActionType") != "ExecuteScript":
                continue
            for match in _SCRIPT_GUID_RE.finditer(action.get("ExecScriptExpression", "")):
                ident = normalized_id(match.group())
                if ident in ids:
                    yield trigger, ident


def _normalize_trigger_ids(root, occupied_ids=(), previous=None):
    folder = root.find("ExportedFolder")
    if folder is None:
        return False
    occupied = {ident for value in occupied_ids if (ident := normalized_id(value))}
    entries = _trigger_locations(root)
    groups = {}
    for trigger, ancestry, location in entries:
        ident = normalized_id(trigger.get("Id"))
        if ident:
            groups.setdefault(ident, []).append((trigger, ancestry, location))
    prior = {}
    prior_names = {}
    prior_sources = {}
    source_counts = Counter(location[1] for _trigger, _ancestry, location in entries)
    if previous is not None:
        if isinstance(previous, (bytes, str)):
            try:
                previous = parse_xml(prepare_import(previous, occupied_ids=occupied))
            except ValueError:
                previous = parse_xml(previous)
        prior = {location: normalized_id(trigger.get("Id"))
                 for trigger, _ancestry, location in _trigger_locations(previous)}
        for trigger, _ancestry, location in _trigger_locations(previous):
            key = location[0], location[1], trigger.get("Name", "")
            prior_names.setdefault(key, []).append(normalized_id(trigger.get("Id")))
            prior_sources.setdefault(location[1], []).append(normalized_id(trigger.get("Id")))
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "NyaaTriggers Triggernometry identities")
    pack_id = normalized_id(folder.get("Id")) or folder.get("Name", "")
    reserved = occupied | set(groups)
    assigned = set()
    replacements = {}
    for trigger, _ancestry, location in entries:
        ident = normalized_id(trigger.get("Id"))
        if not ident:
            continue
        replacement = prior.get(location)
        if replacement is None:
            candidates = prior_names.get((location[0], location[1], trigger.get("Name", "")), [])
            if len(candidates) == 1:
                replacement = candidates[0]
        if replacement is None and source_counts[location[1]] == 1:
            candidates = prior_sources.get(location[1], [])
            if len(candidates) == 1:
                replacement = candidates[0]
        if (replacement in occupied or replacement in assigned
                or replacement != ident and replacement in groups):
            replacement = None
        if replacement is None and len(groups[ident]) == 1 and ident not in occupied and ident not in assigned:
            replacement = ident
        if replacement is None:
            attempt = 0
            while True:
                replacement = str(uuid.uuid5(namespace, repr((pack_id, location, attempt))))
                if replacement not in reserved and replacement not in assigned:
                    break
                attempt += 1
        if replacement != ident:
            replacements[trigger] = replacement
        assigned.add(replacement)
    # References use the nearest containing folder with one matching target.
    references = []
    changed_ids = {ident for ident, targets in groups.items() if any(target[0] in replacements for target in targets)}
    for trigger, ident in script_trigger_references(root, changed_ids):
        raise ValueError(_("Script in {name} contains trigger ID {id}, which must change. Update the script before importing.")
                         .format(name=trigger.get("Name", ""), id=ident))
    for trigger, ancestry, _location in entries:
        for action in trigger.iter("Action"):
            if action.get("ActionType") != "Trigger" or action.get("TriggerOp") == "CancelAllTrigger":
                continue
            ident = normalized_id(action.get("TriggerId"))
            if ident not in changed_ids:
                continue
            targets = groups[ident]
            for scope in reversed(ancestry):
                local = [target for target in targets if scope in target[1]]
                if local:
                    targets = local
                    break
            if len(targets) != 1:
                raise ValueError(_("The trigger reference {id} is ambiguous in {name}. Choose a unique target before importing.")
                                 .format(id=action.get("TriggerId"), name=trigger.get("Name", "")))
            target = targets[0][0]
            references.append((action, replacements.get(target, target.get("Id"))))
    for trigger, ident in replacements.items():
        trigger.set("NyaaSourceId", normalized_id(trigger.get("NyaaSourceId")) or normalized_id(trigger.get("Id")))
        trigger.set("Id", ident)
    for action, ident in references:
        action.set("TriggerId", ident)
    return bool(replacements)


def prepare_import(raw, *, occupied_ids=(), previous=None):
    root = parse_xml(raw)
    if len(root.findall("ExportedFolder")) != 1:
        raise ValueError(_("The file must contain one Triggernometry folder export"))
    folder = root.find("ExportedFolder")
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "NyaaTriggers Triggernometry import")
    occurrences = {}
    changed = False
    for path, trigger, _parent in trigger_entries(root):
        if trigger.get("Id") is not None:
            continue
        key = repr((folder.get("Id", ""), path, ET.tostring(trigger, encoding="unicode")))
        index = occurrences.get(key, 0)
        occurrences[key] = index + 1
        trigger.set("Id", str(uuid.uuid5(namespace, key + ":" + str(index))))
        changed = True
    changed = _normalize_trigger_ids(root, occupied_ids, previous) or changed
    return xml_bytes(root) if changed else raw


def _identity_snapshot(root):
    def copy_folder(folder):
        copied = ET.Element(folder.tag, {name: folder.get(name) for name in ("Id", "Name") if name in folder.attrib})
        triggers = ET.SubElement(copied, "Triggers")
        for trigger in folder.findall("Triggers/Trigger"):
            ET.SubElement(triggers, "Trigger", {name: trigger.get(name) for name in ("Id", "NyaaSourceId", "Name")
                                               if name in trigger.attrib})
        folders = ET.SubElement(copied, "Folders")
        for child in folder.findall("Folders/Folder"):
            folders.append(copy_folder(child))
        return copied

    snapshot = ET.Element("TriggernometryExport")
    snapshot.append(copy_folder(root.find("ExportedFolder")))
    return ET.tostring(snapshot, encoding="unicode")


def _read_identity_history(directory):
    path = directory / _IDENTITY_FILE
    try:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {}, {}
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("packs"), dict)):
            raise ValueError("Invalid identity history")
        roots, occupied = {}, set()
        for name, value in data["packs"].items():
            root = parse_xml(value)
            if len(root.findall("ExportedFolder")) != 1:
                raise ValueError("Invalid identity history")
            ids = [normalized_id(trigger.get("Id")) for _folder, trigger, _parent in trigger_entries(root)]
            if not all(ids) or len(set(ids)) != len(ids) or occupied.intersection(ids):
                raise ValueError("Invalid identity history")
            roots[name] = root
            occupied.update(ids)
        return data["packs"], roots
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise _IdentityHistoryError(_("Could not preserve Triggernometry identities in {path}: {error}")
                                    .format(path=path, error=exc)) from exc


def prepared_packs(paths, *, on_error=None):
    paths = sorted(map(Path, paths))
    prepared, occupied = [], set()
    with _IDENTITY_LOCK:
        histories, roots, failures = {}, {}, {}
        for directory in dict.fromkeys(path.parent for path in paths):
            try:
                histories[directory], roots[directory] = _read_identity_history(directory)
            except (OSError, ValueError, RecursionError) as exc:
                failures[directory] = exc
        updated = {directory: dict(history) for directory, history in histories.items()}
        inputs, input_roots = {}, {}
        for path in paths:
            try:
                if path.parent in failures:
                    raise failures[path.parent]
                with path.open("rb") as stream:
                    inputs[path] = stream.read(MAX_XML_BYTES + 1)
                input_roots[path] = parse_xml(inputs[path])
            except (OSError, ValueError, RecursionError) as exc:
                if on_error is None:
                    raise
                on_error(path, exc)
        for directory, previous in roots.items():
            present = {path.name for path in paths if path.parent == directory}
            incoming = Counter(normalized_id(root.find("ExportedFolder").get("Id"))
                               for path, root in input_roots.items() if path.parent == directory
                               and root.find("ExportedFolder") is not None)
            for path, root in input_roots.items():
                folder = root.find("ExportedFolder")
                ident = normalized_id(folder.get("Id")) if folder is not None else ""
                if path.parent != directory or path.name in previous or not ident or incoming[ident] != 1:
                    continue
                matches = [name for name, old in previous.items() if name not in present
                           and normalized_id(old.find("ExportedFolder").get("Id")) == ident]
                if len(matches) == 1:
                    previous[path.name] = previous.pop(matches[0])
                    updated[directory][path.name] = updated[directory].pop(matches[0])
        for path, raw in inputs.items():
            if path not in input_roots:
                continue
            try:
                # Missing peers retain their IDs so repairs cannot inherit another pack's controls.
                reserved = {normalized_id(trigger.get("Id"))
                            for name, root in roots[path.parent].items() if name != path.name
                            for _folder, trigger, _parent in trigger_entries(root)}
                raw = prepare_import(raw, occupied_ids=occupied | reserved,
                                     previous=roots[path.parent].get(path.name))
                root = parse_xml(raw)
                occupied.update(normalized_id(trigger.get("Id"))
                                for _folder, trigger, _parent in trigger_entries(root))
                updated[path.parent][path.name] = _identity_snapshot(root)
                prepared.append((path, raw))
            except (OSError, ValueError, RecursionError) as exc:
                if on_error is None:
                    raise
                on_error(path, exc)
        for directory, history in updated.items():
            if history == histories[directory]:
                continue
            try:
                from nyaatriggers.app_common import _atomic_write_bytes
                _atomic_write_bytes(directory / _IDENTITY_FILE,
                                    json.dumps({"version": 1, "packs": history}, ensure_ascii=False).encode("utf-8"))
            except OSError as exc:
                error = _IdentityHistoryError(_("Could not preserve Triggernometry identities in {path}: {error}")
                                             .format(path=directory / _IDENTITY_FILE, error=exc))
                if on_error is None:
                    raise error from exc
                for path in paths:
                    if path.parent == directory:
                        on_error(path, error)
                prepared = [(path, raw) for path, raw in prepared if path.parent != directory]
    return prepared


def occupied_import_ids(packs, existing=None):
    packs = Path(packs)
    excluded = existing[0].resolve() if existing is not None else None
    paths = [path for path in packs.iterdir()
             if path.is_file() and path.suffix.lower() == ".xml"]
    errors = []
    prepared = prepared_packs(paths, on_error=lambda _path, exc: errors.append(exc))
    for exc in errors:
        if isinstance(exc, _IdentityHistoryError):
            raise exc
    _history, roots = _read_identity_history(packs)
    return {normalized_id(trigger.get("Id"))
            for name, root in roots.items() if (packs / name).resolve() != excluded
            for _folder, trigger, _parent in trigger_entries(root)} | {
                normalized_id(trigger.get("Id"))
                for path, raw in prepared if path.resolve() != excluded
                for _folder, trigger, _parent in trigger_entries(parse_xml(raw))}


def new_trigger(name=""):
    trigger = ET.Element("Trigger", Id=str(uuid.uuid4()), Name=name or _("New trigger"),
                         Enabled="true", Source="FFXIVNetwork", Sequential="True", RegularExpression="")
    ET.SubElement(trigger, "Actions")
    return trigger


def trigger_entries(root):
    entries = []

    def walk(folder, path):
        path = (*path, folder.get("Name", ""))
        triggers = folder.find("Triggers")
        if triggers is not None:
            entries.extend((" / ".join(path), trigger, triggers) for trigger in triggers if trigger.tag == "Trigger")
        folders = folder.find("Folders")
        if folders is not None:
            for child in folders:
                if child.tag == "Folder":
                    walk(child, path)

    for folder in root.findall("ExportedFolder"):
        walk(folder, ())
    return entries


def validate_pack(root):
    if root.tag != "TriggernometryExport" or len(root.findall("ExportedFolder")) != 1:
        raise ValueError(_("The file must contain one Triggernometry folder export"))
    ids = set()
    for _path, trigger, _parent in trigger_entries(root):
        try:
            ident = str(uuid.UUID(trigger.get("Id", "")))
        except ValueError:
            raise ValueError(_("Every trigger needs a valid ID")) from None
        if ident in ids:
            raise ValueError(_("Duplicate trigger ID: {id}").format(id=ident))
        ids.add(ident)
        if not trigger.get("Name", "").strip():
            raise ValueError(_("Every trigger needs a name"))
        if (trigger.get("IsReadme", "false").strip().lower() != "true"
                and trigger.get("Source", "Log") in ("Log", "FFXIVNetwork")
                and not trigger.get("RegularExpression", "").strip()):
            raise ValueError(_("Enter a regular expression for {name}").format(name=trigger.get("Name")))


def validate_native(raw):
    from nyaatriggers.triggernometry_bridge import _find_exe, _find_mono
    from nyaatriggers.proc_env import child_env
    host = _find_exe()
    validator = host.with_name("triggernometry-validate.exe") if host else None
    mono = _find_mono() if os.name != "nt" else None
    if validator is None or not validator.is_file() or (os.name != "nt" and not mono):
        raise ValueError(_("Install the current Triggernometry engine and its runtime before saving packs"))
    command = ([mono] if mono else []) + [str(validator)]
    try:
        result = subprocess.run(command, input=raw, capture_output=True, timeout=10,
                                env=child_env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ValueError(_("Triggernometry validation timed out. The pack was not saved.")) from None
    if result.returncode or result.stdout.strip() != b"valid":
        error = result.stderr.decode("utf-8", errors="replace")[-4000:]
        raise ValueError(_("Triggernometry rejected this pack: {error}").format(error=error))


class PackDocument:
    def __init__(self, path, raw=None):
        self.path = Path(path)
        self.original = raw
        self._added_ids = set()
        if raw is None:
            self.root = ET.Element("TriggernometryExport", Version="1")
            folder = ET.SubElement(self.root, "ExportedFolder", Id=str(uuid.uuid4()), Name="Unsorted", Enabled="true")
            ET.SubElement(folder, "Triggers")
        else:
            self.root = parse_xml(prepare_import(raw))
            if self.root.find("ExportedFolder") is None:
                raise ValueError(_("The file is not a Triggernometry folder export"))
        self._original_ids = {normalized_id(trigger.get("Id"))
                              for _path, trigger, _parent in trigger_entries(self.root)}

    @classmethod
    def load(cls, path):
        path = Path(path)
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_XML_BYTES + 1)
        document = cls(path, raw)
        paths = [peer for peer in path.parent.iterdir()
                 if peer.is_file() and peer.suffix.lower() == ".xml"]
        errors = []
        for peer, prepared in prepared_packs(paths, on_error=lambda peer, exc: errors.append((peer, exc))):
            if peer == path:
                document.root = parse_xml(prepared)
                document._original_ids = {normalized_id(trigger.get("Id"))
                                          for _folder, trigger, _parent in trigger_entries(document.root)}
                break
        for peer, exc in errors:
            if peer == path:
                raise exc
        return document

    def add_trigger(self, original=None):
        trigger = deepcopy(original) if original is not None else new_trigger()
        if original is not None:
            old_id = normalized_id(trigger.get("Id"))
            trigger.set("Id", str(uuid.uuid4()))
            trigger.set("Name", trigger.get("Name", "") + _(" (copy)"))
            for action in trigger.iter("Action"):
                if old_id and normalized_id(action.get("TriggerId")) == old_id:
                    action.set("TriggerId", trigger.get("Id"))
        folder = self.root.find("ExportedFolder")
        parent = next((parent for _path, item, parent in trigger_entries(self.root) if item is original), None)
        if parent is None:
            parent = folder.find("Triggers")
        if parent is None:
            parent = ET.SubElement(folder, "Triggers")
        parent.append(trigger)
        self._added_ids.add(trigger.get("Id"))
        return trigger

    def save(self):
        from nyaatriggers.app_common import _atomic_write_bytes
        _normalize_trigger_ids(self.root)
        validate_pack(self.root)
        new_ids = {normalized_id(trigger.get("Id")) for _path, trigger, _parent in trigger_entries(self.root)}
        removed = (self._original_ids | self._added_ids) - new_ids
        for action in self.root.iter("Action"):
            if normalized_id(action.get("TriggerId")) in removed:
                raise ValueError(_("A deleted trigger is still referenced by an action. Update that action before saving."))
        if next(script_trigger_references(self.root, removed), None) is not None:
            raise ValueError(_("A deleted trigger is still referenced by a script. Update that script before saving."))
        raw = xml_bytes(self.root)
        validate_native(raw)
        if self.path.exists():
            with self.path.open("rb") as stream:
                current = stream.read(MAX_XML_BYTES + 1)
        else:
            current = None
        if current != self.original:
            raise ValueError(_("This pack changed outside the editor. Reopen it before saving."))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.original is not None:
            _atomic_write_bytes(pack_backup_path(self.path), self.original)
        _atomic_write_bytes(self.path, raw)
        self.original = raw
        self._original_ids = new_ids
        self._added_ids.clear()
