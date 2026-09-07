from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REQUIRED_FIELDS = {
    "schemaVersion",
    "displayName",
    "displayNameStatus",
    "internalProductId",
    "version",
    "architecture",
    "publisher",
    "repositorySlug",
    "executableName",
    "brokerExecutableName",
    "installerAppId",
    "settingsRelativePath",
    "installRelativePath",
    "copyright",
    "minimumWindows",
    "releaseChannel",
    "installerArchitecture",
    "aiAgentEntryPoint",
    "aiAgentProtocol",
}


def load_product(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != REQUIRED_FIELDS:
        raise ValueError("product.json has unexpected fields")
    if value["schemaVersion"] != "1.0.0":
        raise ValueError("unsupported product schema")
    if value["internalProductId"] != "AILimitsWidget":
        raise ValueError("stable internal product ID changed")
    if value["displayNameStatus"] != "temporary-working-name":
        raise ValueError("working-name status was softened")
    if value["architecture"] != "x64":
        raise ValueError("version 1 supports x64 only")
    if value["installerArchitecture"] != "signing-ready-explicitly-unsigned-preview":
        raise ValueError("installer architecture identity changed")
    if value["aiAgentEntryPoint"] != "LimitHalo-Agent.ps1":
        raise ValueError("AI agent entry-point identity changed")
    if value["aiAgentProtocol"] != "limit-halo-agent/1":
        raise ValueError("AI agent protocol identity changed")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(value["version"])):
        raise ValueError("version must be three-part SemVer")
    return value


def version_tuple(version: str) -> tuple[int, int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    if any(part > 65535 for part in (major, minor, patch)):
        raise ValueError("version component exceeds Win32 range")
    return major, minor, patch, 0


def escape_pyinstaller(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def write_pyinstaller_version(product: dict[str, object], output: Path) -> None:
    version = str(product["version"])
    numeric = version_tuple(version)
    strings = {
        "CompanyName": str(product["publisher"]),
        "FileDescription": f"{product['displayName']} transparent quota HUD",
        "FileVersion": version,
        "InternalName": str(product["internalProductId"]),
        "LegalCopyright": str(product["copyright"]),
        "OriginalFilename": str(product["executableName"]),
        "ProductName": str(product["displayName"]),
        "ProductVersion": version,
    }
    structs = ",\n".join(
        f"        StringStruct('{escape_pyinstaller(key)}', '{escape_pyinstaller(value)}')"
        for key, value in strings.items()
    )
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric},
    prodvers={numeric},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
{structs}
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    output.write_text(text, encoding="utf-8", newline="\n")


def write_application_manifest(output: Path, *, dpi_aware: bool) -> None:
    dpi = """
      <asmv3:application>
        <asmv3:windowsSettings>
          <dpiAware xmlns=\"http://schemas.microsoft.com/SMI/2005/WindowsSettings\">true/pm</dpiAware>
          <dpiAwareness xmlns=\"http://schemas.microsoft.com/SMI/2016/WindowsSettings\">PerMonitorV2</dpiAwareness>
        </asmv3:windowsSettings>
      </asmv3:application>""" if dpi_aware else ""
    text = f"""<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<assembly xmlns=\"urn:schemas-microsoft-com:asm.v1\" xmlns:asmv3=\"urn:schemas-microsoft-com:asm.v3\" manifestVersion=\"1.0\">
  <trustInfo xmlns=\"urn:schemas-microsoft-com:asm.v3\">
    <security><requestedPrivileges><requestedExecutionLevel level=\"asInvoker\" uiAccess=\"false\"/></requestedPrivileges></security>
  </trustInfo>
  <compatibility xmlns=\"urn:schemas-microsoft-com:compatibility.v1\">
    <application>
      <supportedOS Id=\"{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}\"/>
      <supportedOS Id=\"{{4f476546-937d-4f5d-87f6-5c6f6f65c3eb}}\"/>
    </application>
  </compatibility>{dpi}
</assembly>
"""
    output.write_text(text, encoding="utf-8", newline="\n")


def rc_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def write_broker_rc(product: dict[str, object], output: Path) -> None:
    version = str(product["version"])
    numeric = ",".join(str(part) for part in version_tuple(version))
    text = f"""#include <windows.h>

1 VERSIONINFO
 FILEVERSION {numeric}
 PRODUCTVERSION {numeric}
 FILEFLAGSMASK 0x3fL
 FILEFLAGS 0x0L
 FILEOS 0x40004L
 FILETYPE 0x1L
 FILESUBTYPE 0x0L
BEGIN
  BLOCK \"StringFileInfo\"
  BEGIN
    BLOCK \"040904B0\"
    BEGIN
      VALUE \"CompanyName\", \"{rc_string(str(product['publisher']))}\"
      VALUE \"FileDescription\", \"{rc_string(str(product['displayName']))} Claude quota broker\"
      VALUE \"FileVersion\", \"{version}\"
      VALUE \"InternalName\", \"AILimitsWidgetClaudeBroker\"
      VALUE \"LegalCopyright\", \"{rc_string(str(product['copyright']))}\"
      VALUE \"OriginalFilename\", \"{rc_string(str(product['brokerExecutableName']))}\"
      VALUE \"ProductName\", \"{rc_string(str(product['displayName']))}\"
      VALUE \"ProductVersion\", \"{version}\"
    END
  END
  BLOCK \"VarFileInfo\"
  BEGIN
    VALUE \"Translation\", 0x0409, 1200
  END
END
"""
    output.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    product_path = args.product.resolve(strict=True)
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    product = load_product(product_path)

    widget_manifest = output / "AILimitsWidget.manifest"
    broker_manifest = output / "ClaudeUsageBroker.manifest"
    write_application_manifest(widget_manifest, dpi_aware=True)
    write_application_manifest(broker_manifest, dpi_aware=False)
    write_pyinstaller_version(product, output / "AILimitsWidget.version.txt")
    write_broker_rc(product, output / "ClaudeUsageBroker.version.rc")


if __name__ == "__main__":
    main()
