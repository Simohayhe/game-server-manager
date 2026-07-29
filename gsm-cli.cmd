@echo off
rem GSM console (CLI).  e.g.  gsm-cli status  /  gsm-cli start minecraft4
rem remote:  gsm-cli --url http://<host>:8770 --token <token> status
py -3.12 "%~dp0main_app.py" --cli %*
