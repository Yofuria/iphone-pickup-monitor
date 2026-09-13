#!/bin/zsh
cd -- "${0:A:h}" || exit 1
print '显示最新监控日志。按 Ctrl+C 关闭日志查看，后台监控会继续运行。'
tail -n 30 -F runtime/monitor.log
