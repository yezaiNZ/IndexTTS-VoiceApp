; A harmless OLD installer for the upgrade integration tests. NOT a release.
#ifndef UpgradeTestRoot
  #error UpgradeTestRoot is required
#endif
#ifndef BuildVersion
  #error BuildVersion is required
#endif
[Setup]
AppId={{64FC1CE2-89FD-451C-B145-8CFBF7BCF6D0}
AppName=IndexTTS Voice App Upgrade Test
AppVersion={#BuildVersion}
DefaultDirName={#UpgradeTestRoot}\IndexTTS Voice App v{#BuildVersion}
UsePreviousAppDir=no
OutputDir={#UpgradeTestRoot}\packages
OutputBaseFilename=Legacy-Test-{#BuildVersion}
PrivilegesRequired=lowest
UninstallDisplayName=IndexTTS Voice App Upgrade Test
Compression=lzma2
SolidCompression=yes
[Files]
Source: "{#UpgradeTestRoot}\payload\*"; DestDir: "{app}"; Flags: ignoreversion
