@echo off
chcp 65001 > nul
echo =============================================
echo  법률 문서 플랫폼 시작
echo =============================================

:: 백엔드 실행
echo [1/2] 백엔드 서버 시작...
cd /d "%~dp0backend"
if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo  .env 파일을 생성했습니다. OPENAI_API_KEY를 입력하세요.
    notepad .env
)
start "Backend" cmd /k "pip install -r requirements.txt -q && uvicorn main:app --reload --port 8000"

:: 잠시 대기
timeout /t 3 /noisy > nul

:: 프론트엔드 실행
echo [2/2] 프론트엔드 서버 시작...
cd /d "%~dp0frontend"
start "Frontend" cmd /k "npm install && npm run dev"

echo.
echo 브라우저에서 http://localhost:5173 열기...
timeout /t 5 /noisy > nul
start http://localhost:5173

echo.
echo 서버가 실행 중입니다.
echo 종료하려면 각 창을 닫으세요.
pause
