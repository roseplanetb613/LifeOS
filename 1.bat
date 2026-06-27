@echo off
echo ================================
echo Unity Git 初始化脚本
echo ================================

REM 进入当前目录
cd /d %~dp0

echo [1/6] 初始化Git仓库...
git init

echo [2/6] 创建 .gitignore ...

(
echo [Ll]ibrary/
echo [Tt]emp/
echo [Oo]bj/
echo [Bb]uild/
echo [Bb]uilds/
echo [Ll]ogs/
echo [Uu]serSettings/
echo .vs/
echo *.csproj
echo *.unityproj
echo *.sln
echo *.user
echo *.userprefs
echo .DS_Store
) > .gitignore

echo [3/6] 添加所有文件...
git add .

echo [4/6] 首次提交...
git commit -m "init unity project"

echo [5/6] 设置默认分支为 main...
git branch -M main

echo ================================
echo Git 初始化完成！
echo ================================
pause