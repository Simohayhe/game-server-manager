@echo off
rem Connect-only GUI (attach to an existing/remote service; does NOT start one).
rem   gsm-connect                                  -> local 127.0.0.1:8770
rem   gsm-connect http://192.168.11.5:8770 --token <token>
start "" pyw -3.12 "%~dp0main_app.py" --connect %*
