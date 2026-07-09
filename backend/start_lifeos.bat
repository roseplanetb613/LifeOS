@echo off
chcp 65001 >nul
title LifeOS Server

echo ============================================
echo   LifeOS Backend
echo   Dashboard: http://localhost:8000/dashboard
echo   Memory:    http://localhost:8000/memory
echo   API Docs:  http://localhost:8000/docs
echo ============================================
echo.

call conda activate sharp
if errorlevel 1 (
    echo [ERROR] conda environment 'sharp' not found!
    pause
    exit /b 1
)

echo Starting server...
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

pause
