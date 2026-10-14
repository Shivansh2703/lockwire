"""Lockwire never actuates. Probes are read-only; it must never write to a
device node or bus, and any ROS publisher is either read-only telemetry
explicitly allowlisted below, by name and reason, or a defect.

Repo-wide guard: greps every module under ``src/lockwire`` (the private repo also
sweeps its ROS and packaging trees) for the four device-write / actuation shapes the never-actuate rule forbids. Each
shape has its OWN synthetic test proving its regex actually matches something
— a matcher with no synthetic is believed, not proven, and this file used to
carry only one.

Four shapes:
  1. ``_DEV_WRITE_OPEN`` — a path under /dev opened for writing.
  2. ``_BUS_IMPORT`` + ``_WRITE_CALL`` — a bus library (smbus/smbus2/pyserial)
     imported and used to write to it.
  3. ``_RAW_WRITE`` — raw device-level write syscalls (os.write/ioctl).
  4. ``_CREATE_PUBLISHER`` / ``_PUBLISH_METHOD`` — a ROS publisher created or
     used. Publishing to a topic is the ROS-native form of actuation, and it
     matches none of shapes 1-3. Every publisher found this way must be
     covered by an entry in ``_PUBLISHER_ALLOWLIST`` below, keyed by
     (file, message type, topic-argument SOURCE SPELLING as written at the
     call site — this is a text-level pin, not a runtime read). The entry says
     why it may publish and whether that spelling is a literal whose runtime
     value the pin therefore proves (``verified: True``) or an identifier
     whose runtime value the pin does NOT prove (``verified: False`` — see
     Ceiling below). A publisher whose exact signature is not listed fails the
     sweep — so the allowlist is the thing that turns "nobody looked" into
     "someone decided this may publish."

Ceiling (named, not chased): this is a text-level grep, not an AST/dataflow
guard, and it does not resolve identifiers to runtime values.
  - **The topic pin is a source-spelling pin, not a value read.** When an
    allowlisted call's topic argument is a string literal (e.g.
    ``test_command_relay.py``), the pinned spelling IS the runtime topic, so
    ``verified: True`` is an honest claim. When it is an identifier (e.g.
    ``gate_node.py``'s ``create_publisher(DiagnosticArray, topic, 10)``,
    where ``topic`` comes from a ``declare_parameter`` default that a ROS
    launch file, a YAML config, or a CLI ``-p`` override can change at
    runtime with zero text-level trace in this file), the pin proves only
    that the CALL SITE has not changed — the actual destination topic is
    **NOT VERIFIED** by this guard. See each entry's ``verified`` field and
    reason text below; do not read the ``topic`` string in an entry as "this
    is where it publishes" unless ``verified`` says so.
  - **Five further actuation spellings this sweep cannot see at all**
    (reasoned from what each matcher above requires — none is hypothetical):
    a ROS **service client** (``create_client(...)`` + ``.call_async(``); a
    ROS **action client** (``ActionClient(...)`` + ``.send_goal(``); a
    **shelled-out ROS CLI publish**
    (``subprocess.run(["ros2", "topic", "pub", ...])``); a **shelled-out bus
    write** (``os.system("i2cset ...")``); and a **pathlib device write**
    (``Path("/dev/i2c-1").write_bytes(...)``). None of the four shapes above
    match any of these five.
  - An indirectly-built /dev path still defeats shape 1.
  - A ``.publish(`` call on an object built or passed in elsewhere in the
    file, with no local ``create_publisher(`` to trace it to, cannot be
    matched to a message type or topic — such a file is flagged as an
    offender outright (the final check inside ``_publisher_offenders``
    below) rather than silently passed.
"""

import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).parent.parent
SRC = REPO / "src" / "lockwire"
SRC_PRO = REPO / "src" / "lockwire_pro"  # absent in the public repo

# A path under /dev opened for writing, e.g. open("/dev/i2c-1", "wb").
_DEV_WRITE_OPEN = re.compile(r"""open\([^)]*/dev/[^)]*,\s*["'](w|wb|r\+)["']""")
# A bus library (smbus/smbus2/pyserial) imported and used to write to it —
# any .write-prefixed call, matched by shape (not by enumerating method
# names — an enumeration always misses the next one; write_i2c_block_data
# escaped a name-list guard twice because it has a digit in it).
# .write_timeout (a bare attribute, no call) correctly does not match.
_BUS_IMPORT = re.compile(r"^\s*(import|from)\s+(smbus2?|serial)\b", re.MULTILINE)
_WRITE_CALL = re.compile(r"\.write\w*\(")
# Raw device-level write syscalls.
_RAW_WRITE = re.compile(r"\bos\.write\(|\bioctl\(")
# A ROS publisher created, or used. Captures (message type, topic-as-written)
# for the allowlist check.
_CREATE_PUBLISHER = re.compile(r"create_publisher\(\s*([\w.]+)\s*,\s*([^,]+?)\s*,")
_PUBLISH_METHOD = re.compile(r"\.publish\(")

# (relative file path, ROS message type, topic as written at the call site) -> entry.
# Nothing under src/lockwire publishes, so the allowlist is empty here; the private
# repo's copy lists the ROS-package publishers it also sweeps.
_PUBLISHER_ALLOWLIST: dict = {}


# A root that stops matching (a rename, a move a level deeper) must not go
# silently invisible behind the other roots' counts — "no offenders found"
# must not survive "no files examined". One conservative floor per root,
# well under the measured counts (66, 16, 2) so a few added files never
# flakes this.
_MIN_FILES = {SRC: 40}


def _iter_py_files(*roots):
    for root in roots:
        if root.exists():
            yield from root.rglob("*.py")


def test_swept_roots_actually_contain_python_files():
    for root, minimum in _MIN_FILES.items():
        count = sum(1 for _ in root.rglob("*.py"))
        assert count >= minimum, (
            f"{_relpath(root)} enumerated only {count} .py files (expected >= "
            f"{minimum}) — the sweep is passing because it found nothing to check"
        )


def _relpath(path: Path) -> str:
    return str(path.relative_to(REPO))


def _device_write_offenders(roots):
    offenders = []
    for path in _iter_py_files(*roots):
        text = path.read_text()
        hits_dev_write = _DEV_WRITE_OPEN.search(text)
        hits_bus_write = _BUS_IMPORT.search(text) and _WRITE_CALL.search(text)
        hits_raw_write = _RAW_WRITE.search(text)
        if hits_dev_write or hits_bus_write or hits_raw_write:
            offenders.append(_relpath(path))
    return offenders


def _publisher_count_offenders(creation_counts):
    """Reject a missing or duplicate call for every named publisher exception."""
    offenders = []
    for (rel, msg_type, topic), entry in _PUBLISHER_ALLOWLIST.items():
        actual = creation_counts[(rel, msg_type, topic)]
        if actual != entry["count"]:
            offenders.append(
                f"{rel}: {msg_type} publisher on {topic} count {actual} disagrees with "
                f"its allowlist count {entry['count']}"
            )
    return offenders


def _publisher_offenders(roots):
    """Every create_publisher(...) not covered by the allowlist, or covered
    by name but publishing a different topic than the entry names; plus any
    file with a `.publish(` call it cannot trace to a local create_publisher."""
    offenders = []
    creation_counts = Counter()
    for path in _iter_py_files(*roots):
        text = path.read_text()
        rel = _relpath(path)
        creations = _CREATE_PUBLISHER.findall(text)
        for msg_type, topic in creations:
            signature = (rel, msg_type, topic)
            entry = _PUBLISHER_ALLOWLIST.get(signature)
            if entry is None:
                offenders.append(f"{rel}: unlisted publisher of {msg_type} on topic {topic!r}")
            else:
                creation_counts[signature] += 1
        if _PUBLISH_METHOD.search(text) and not creations:
            offenders.append(f"{rel}: .publish( call with no local create_publisher to trace it to")
    offenders.extend(_publisher_count_offenders(creation_counts))
    return offenders


def test_dev_write_open_pattern_catches_a_device_write():
    assert _DEV_WRITE_OPEN.search('open("/dev/i2c-1", "wb")')


def test_write_call_pattern_catches_write_i2c_block_data():
    # Pinned by name: this exact shape escaped two prior name-list guards
    # because it has a digit ("i2c") the old character class couldn't cross.
    assert _WRITE_CALL.search("smbus2.SMBus(1).write_i2c_block_data(0x40, 0, [1, 2])")


def test_raw_write_pattern_catches_os_write_and_ioctl():
    assert _RAW_WRITE.search("os.write(fd, data)")
    assert _RAW_WRITE.search("ioctl(fd, REQUEST, arg)")


def test_create_publisher_pattern_catches_the_call():
    match = _CREATE_PUBLISHER.search('self.create_publisher(DiagnosticArray, "/diagnostics", 10)')
    assert match
    assert match.group(1) == "DiagnosticArray"
    assert match.group(2) == '"/diagnostics"'


def test_publish_method_pattern_catches_the_call():
    assert _PUBLISH_METHOD.search("publisher.publish(message)")


def test_no_module_writes_to_a_device():
    offenders = _device_write_offenders([r for r in (SRC, SRC_PRO) if r.exists()])
    assert not offenders, (
        f"never-actuate violation: module(s) with a device-write shape (never actuate): {offenders}"
    )


def test_every_ros_publisher_is_named_on_the_allowlist():
    offenders = _publisher_offenders([r for r in (SRC, SRC_PRO) if r.exists()])
    assert not offenders, (
        f"never-actuate violation: publisher(s) not covered by a named, reasoned allowlist entry: {offenders}"
    )


def _is_source_literal(topic_source: str) -> bool:
    """True iff a create_publisher topic argument, as captured from source
    text, is a plain quoted string literal rather than an identifier/
    expression whose runtime value this text-level guard cannot see."""
    return bool(re.fullmatch(r"""(["']).*\1""", topic_source))
