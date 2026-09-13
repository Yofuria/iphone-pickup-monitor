#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  print '请先安装 Python 3.9 或更新版本。'
  read '?按回车退出'
  exit 1
fi
print '正在监控北京 iPhone 18 Pro 银色 256GB＋全部 18 Pro Max。按 Ctrl+C 停止。'
print '保持电脑联网、开盖；此窗口关闭后监控停止。'
/usr/bin/caffeinate -i python3 monitor.py
read '?监控已停止，按回车关闭窗口'
