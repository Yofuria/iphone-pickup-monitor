#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 monitor.py --stop
read '?按回车关闭窗口'
