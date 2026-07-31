on run
  set controlPath to POSIX path of (path to resource "control.sh")
  set choices to {"启动 / 恢复守望器", "暂停守望器", "查看状态", "安全扫描（不续接）", "打开日志", "卸载守望器"}
  set picked to choose from list choices with title "Codex 熔断续聊" with prompt "CC Switch 恢复后，自动续接因熔断中断的 Codex 任务。" default items {"查看状态"} OK button name "执行" cancel button name "关闭"
  if picked is false then return

  set actionName to item 1 of picked
  if actionName is "启动 / 恢复守望器" then
    set commandName to "start"
  else if actionName is "暂停守望器" then
    set commandName to "pause"
  else if actionName is "查看状态" then
    set commandName to "status"
  else if actionName is "安全扫描（不续接）" then
    set commandName to "inspect"
  else if actionName is "打开日志" then
    set commandName to "logs"
  else
    set answer to display dialog "确认卸载守望器？配置和日志会移到废纸篓，可恢复。" buttons {"取消", "卸载"} default button "取消" with icon caution
    if button returned of answer is not "卸载" then return
    set commandName to "uninstall"
  end if

  try
    set resultText to do shell script quoted form of controlPath & " " & commandName
    display dialog resultText with title "Codex 熔断续聊" buttons {"好"} default button "好"
  on error errorText number errorNumber
    display dialog "执行失败（" & errorNumber & "）：" & return & errorText with title "Codex 熔断续聊" buttons {"好"} default button "好" with icon stop
  end try
end run
