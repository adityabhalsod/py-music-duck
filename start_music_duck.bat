@echo off

cd /d "C:\Users\adity\Documents\Projects\music-duck"

call env\Scripts\activate.bat

python auto_duck.py --device-index 1 --duck-volume 15 --restore-delay 2

pause