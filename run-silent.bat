@echo off
chcp 65001 >nul 2>&1
cd /d "C:\Users\Computer\Desktop\checkin"
echo. >> "C:\Users\Computer\Desktop\checkin\signin.log"
echo === %date% %time% === >> "C:\Users\Computer\Desktop\checkin\signin.log"
"C:\Users\Computer\.workbuddy\binaries\python\versions\3.13.12\python.exe" "C:\Users\Computer\Desktop\checkin\signin.py" auto >> "C:\Users\Computer\Desktop\checkin\signin.log" 2>&1
