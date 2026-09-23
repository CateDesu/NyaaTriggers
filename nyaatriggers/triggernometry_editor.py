"""Edit native Triggernometry packs without discarding unexposed settings."""

from copy import deepcopy
from pathlib import Path
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET

from nyaatriggers.convert_triggernometry import MAX_XML_BYTES
from nyaatriggers.locale_util import _


def normalized_id(value):
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return ""


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
        if trigger.get("Source", "Log") in ("Log", "FFXIVNetwork") and not trigger.get("RegularExpression", "").strip():
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
            self.root = parse_xml(raw)
            if self.root.find("ExportedFolder") is None:
                raise ValueError(_("The file is not a Triggernometry folder export"))

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            return cls(path, stream.read(MAX_XML_BYTES + 1))

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
        validate_pack(self.root)
        old_ids = {normalized_id(trigger.get("Id")) for _path, trigger, _parent in trigger_entries(parse_xml(self.original))} if self.original is not None else set()
        new_ids = {normalized_id(trigger.get("Id")) for _path, trigger, _parent in trigger_entries(self.root)}
        removed = (old_ids | self._added_ids) - new_ids
        for action in self.root.iter("Action"):
            if normalized_id(action.get("TriggerId")) in removed:
                raise ValueError(_("A deleted trigger is still referenced by an action. Update that action before saving."))
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
            _atomic_write_bytes(self.path.with_suffix(".xml.bak"), self.original)
        _atomic_write_bytes(self.path, raw)
        self.original = raw
        self._added_ids.clear()
