# Probation Evaluation System

수습평가 운영을 위한 Flask 기반 웹앱입니다.

## 1) 로컬 실행

```bash
python -m pip install -r requirements.txt
python app.py
```

브라우저: `http://127.0.0.1:5000/login`

## 2) GitHub 업로드

프로젝트 폴더에서:

```bash
git init
git add .
git commit -m "Initial probation evaluation system"
git branch -M main
git remote add origin https://github.com/<github-id>/<repo-name>.git
git push -u origin main
```

## 3) Railway 배포

1. Railway에서 **New Project -> Deploy from GitHub repo** 선택
2. 방금 업로드한 저장소 연결
3. 환경변수 설정
   - `SECRET_KEY`: 임의의 긴 문자열
   - `GEMINI_API_KEY`: AI 질문 생성 사용 시
   - `DATABASE_URL`: Railway PostgreSQL 연결 문자열 (Postgres 추가 시 자동 주입 가능)
4. `Start Command`는 `railway.json`/`Procfile`로 자동 인식

## 4) 운영 시 주의

- 앱은 `DATABASE_URL`이 있으면 PostgreSQL을 자동 사용하고, 없으면 SQLite를 사용합니다.
- Railway 운영 배포에서는 PostgreSQL 서비스 연결을 권장합니다.
- `.gitignore`에 DB/업로드 파일은 제외되어 있습니다.

