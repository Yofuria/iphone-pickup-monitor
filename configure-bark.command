#!/bin/zsh
cd -- "${0:A:h}" || exit 1
print '请先在 iPhone 安装 Bark 并允许通知。'
print '输入基础地址：https://api.day.app/你的Key（去掉后面的测试推送文字）。'
if [[ -s .bark-url ]]; then
  print '已发现现有手机配置；新地址会追加，不会覆盖。'
  python3 monitor.py --add-bark && python3 monitor.py --test-notify
else
  python3 monitor.py --setup-bark && python3 monitor.py --test-notify
fi
read '?按回车关闭窗口'
