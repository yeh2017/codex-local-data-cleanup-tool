Option Explicit

Dim shell, fso, folder, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & folder & "\diagnose_codex_cleanup_tool.bat" & Chr(34) & " --silent"
exitCode = shell.Run(command, 0, True)

If exitCode <> 0 Then
    MsgBox "Startup failed. Run diagnose_codex_cleanup_tool.bat for diagnostics.", 16, "Codex Local Data Cleanup Tool"
End If
