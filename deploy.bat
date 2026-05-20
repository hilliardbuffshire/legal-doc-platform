@echo off
chcp 65001 > nul
echo =============================================
echo  Railway 배포 스크립트
echo =============================================
echo.

:: Railway CLI 설치 확인
where railway >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Railway CLI가 없습니다. 설치합니다...
    npm install -g @railway/cli
)

:: Git 초기화 (없으면)
if not exist ".git" (
    echo Git 초기화...
    git init
    git add .
    git commit -m "initial commit"
)

echo.
echo [1단계] Railway 로그인...
railway login

echo.
echo [2단계] 프로젝트 연결 (처음이면 새 프로젝트 생성)...
railway init

echo.
echo [3단계] 환경변수 설정...
echo OPENAI_API_KEY 입력:
set /p OKEY="> "
railway variables set OPENAI_API_KEY=%OKEY%

echo ACCESS_KEY 입력 (변호사와 공유할 비밀번호):
set /p AKEY="> "
railway variables set ACCESS_KEY=%AKEY%

echo DATA_DIR=/data
railway variables set DATA_DIR=/data

echo.
echo [4단계] 볼륨 생성 (파일 영구 저장용)...
echo ※ Railway 대시보드에서 직접 생성하세요:
echo    1. railway.app 접속 → 프로젝트 클릭
echo    2. 우측 상단 "Add Volume" 클릭
echo    3. Mount Path: /data  입력 후 저장
echo.
pause

echo.
echo [5단계] 배포 시작...
railway up --detach

echo.
echo [6단계] 도메인 확인...
railway domain

echo.
echo =============================================
echo  배포 완료!
echo  위 URL을 변호사와 공유하세요.
echo  접속 시 ACCESS_KEY 입력 필요.
echo =============================================
pause
