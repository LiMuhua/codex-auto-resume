' Double-click entry point with no console window, even when .pyw files have no
' file association. Runs the sibling start.pyw with pythonw.exe (or the pyw.exe
' launcher), passing any command-line arguments through unchanged.
Option Explicit
Dim shell, fso, launcher, command, extra, i
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
launcher = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "start.pyw")
command = Chr(34) & launcher & Chr(34)
For i = 0 To WScript.Arguments.Count - 1
    command = command & " " & Chr(34) & Replace(WScript.Arguments(i), Chr(34), Chr(34) & Chr(34)) & Chr(34)
Next

On Error Resume Next
shell.Run "pythonw.exe " & command, 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "pyw.exe -3 " & command, 0, False
End If
If Err.Number <> 0 Then
    MsgBox "pythonw.exe or pyw.exe was not found. Install Python, or run:" & vbCrLf & "python start\start.pyw", 16, "Codex Relay"
End If
