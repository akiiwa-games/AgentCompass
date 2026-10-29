# Signing the tray installer

The default build is unsigned. There is no certificate in this repository.
Windows SmartScreen will warn the first time someone runs `AgentCompass-tray-setup.exe`
or `AgentCompass.exe`. That warning is expected until a publisher certificate is used.

`packaging/build.ps1` signs only when both of these are set:

- `AGENTCOMPASS_SIGNTOOL` — full path to `signtool.exe`
- one of:
  - `AGENTCOMPASS_CERT_THUMBPRINT` — certificate in the current user's store
  - `AGENTCOMPASS_CERT_PFX` plus `AGENTCOMPASS_CERT_PASSWORD`

The command is:

```text
signtool sign /fd SHA256 /td SHA256 /tr http://timestamp.digicert.com /sha1 %AGENTCOMPASS_CERT_THUMBPRINT% AgentCompass.exe
```

A PFX build uses `/f` and `/p` instead of `/sha1`. When the variables are unset, the script
prints the SmartScreen note and leaves the files unsigned. It never launches the tray executable.
