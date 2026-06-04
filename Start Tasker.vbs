' Silent launcher for Claude Tasker — no console flash at all.
Set sh = CreateObject("WScript.Shell")
base = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "pythonw """ & base & "claude_tasker.pyw""", 0, False
