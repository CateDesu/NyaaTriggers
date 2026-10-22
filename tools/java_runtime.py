"""Apply and check the bundled Java certificate distrust data."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


# Includes cross-signed variants from https://emudhra.we-have-a-problem.com/impacted-intermediates.json.
EMUDHRA_CERTIFICATES = frozenset("""
42C4ACCD12C4F59597925E1A08A7A90102608543809308D50EC427E118BA1493
45A883F349AEDFD5F3A649B5498FFB121712BA8B448F3F2A91DBC5A766FCDDBE
98B896625C0C230ADEEC3EB2CF6AAD5001ECF9156A68644E82374CE5833BC25B
5F1DED4DD590AF74631290CF073B122A2AAC6A1FA479778A0BD43579BD832699
E7568FE6E12331F4FD7DE61DA4C7559D0F8EB192A218D4741A3BA75184333838
579480AEA166290BD3C643589E795FC0D0E54E86505D180F1DFF99EFD3199512
0164D7CEAE5BB909C272AF151FF44B1177AE9B47D9424B94F1D9558C30934D19
62B7F9868263F72F6911FC78CEAF72C1979158B7368BF648683BB2AA39B02E72
CB6E15165AC802E9B3F993F16C25D41F4E81C21EBDE71AE1B012AC00BF9C7C3F
7485B51C0ED192878FA1D2F948FB529AE1842CC5086C4B67F7BE5B2039DE9489
432EC25F0D28DE9006D97BF752F17BBB2CD41F669933FCB072D9A1148D0261F3
FD9D5C2DBBD4995AB69B8D50C439D9527B00529199C2FD01B2174EB58F07021F
9E39DE2881273ACC225A149C72BA3E0F51424963669AB633EE1DC7DF5DB067B4
A496E23AC0CAB8155FC9B117B97EAE368EFCBB9041AEC8804D5A308D2D01C2D8
4B2460A466F95FD40113BCDCCA2CEB29B3B093921DE45474DA7DF48874C630B1
84112972B8E59D14560AE7E7119654474AAD119990CF3722BFC1E3B91615E1FD
""".split())


def read_blocklist(data):
    algorithm = None
    certificates = set()
    for line in data.decode("latin-1").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if match := re.fullmatch(r"Algorithm\s*[=:\s]\s*(\S+)", line):
            if algorithm is not None and algorithm != match[1]:
                raise ValueError("Conflicting blocklist algorithms")
            algorithm = match[1]
        elif match := re.fullmatch(r"([0-9A-F]{64})(?:\s*[=:\s].*)?", line):
            certificates.add(match[1])
        else:
            raise ValueError("Invalid Java certificate blocklist entry")
    if algorithm != "SHA-256":
        raise ValueError("Expected the Java SHA-256 certificate blocklist format")
    return certificates


def mitigate(runtime, official_file=None):
    official = read_blocklist(official_file.read_bytes()) if official_file else EMUDHRA_CERTIFICATES
    if not EMUDHRA_CERTIFICATES <= official:
        raise ValueError("Official blocklist is missing compromised eMudhra certificates")
    target = runtime / "lib/security/blocked.certs"
    original = target.read_bytes()
    existing = read_blocklist(original)
    missing = official - existing
    if missing:
        backup = target.with_suffix(".certs.bak")
        if not backup.exists():
            shutil.copy2(target, backup)
        addition = "\n# https://ops.java/emudhra\n" + "\n".join(sorted(missing)) + "\n"
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
            prepared = Path(staged.name)
        try:
            prepared.write_bytes(original + addition.encode("ascii"))
            shutil.copymode(target, prepared)
            prepared.replace(target)
        finally:
            prepared.unlink(missing_ok=True)
    print(f"Java blocklist includes all 16 compromised certificates and preserves {len(existing)} existing entries")


def verify(runtime, jar, version):
    # Read the JVM's loaded distrust properties through the engine's existing Groovy runtime.
    script = """
        if (System.getProperty('java.runtime.version') != args[0]) {
            throw new IllegalStateException('Unexpected bundled Java: ' + System.getProperty('java.runtime.version'))
        }
        if (new File(System.getProperty('java.home')).canonicalFile != new File(args[2]).canonicalFile) {
            throw new IllegalStateException('Java loaded a different runtime directory')
        }
        def type = Class.forName('sun.security.util.UntrustedCertificates')
        def field = type.getDeclaredField('props')
        field.setAccessible(true)
        def properties = field.get(null)
        if (properties.getProperty('Algorithm') != 'SHA-256' ||
                !args[1].split(',').every { properties.containsKey(it) }) {
            throw new IllegalStateException('Bundled Java did not load all eMudhra distrust entries')
        }
        println 'PASS bundled Java ' + args[0] + ' loads all 16 eMudhra distrust entries from ' + System.getProperty('java.home')
    """
    java = runtime / "bin" / ("java.exe" if (runtime / "bin/java.exe").is_file() else "java")
    subprocess.run([str(java.resolve()), "--add-opens=java.base/sun.security.util=ALL-UNNAMED",
                    "-cp", str(jar.resolve()), "groovy.ui.GroovyMain", "-e", script,
                    version, ",".join(sorted(EMUDHRA_CERTIFICATES)), str(runtime.resolve())],
                   check=True, timeout=60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    apply = commands.add_parser("mitigate")
    apply.add_argument("runtime", type=Path)
    apply.add_argument("official_file", type=Path, nargs="?")
    check = commands.add_parser("verify")
    check.add_argument("runtime", type=Path)
    check.add_argument("jar", type=Path)
    check.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.command == "mitigate":
        mitigate(args.runtime, args.official_file)
    else:
        verify(args.runtime, args.jar, args.version)


if __name__ == "__main__":
    main()
