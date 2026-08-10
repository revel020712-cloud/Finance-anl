# Render.com 배포 가이드 (약 5분)

제가 로그인이 필요한 GitHub/Render 계정 작업을 대신 해드릴 수는 없어서(비밀번호 입력·계정 생성은 안전 정책상 제가 직접 하지 않습니다), 아래 순서 그대로 따라 하시면 됩니다. 필요한 설정 파일(`render.yaml`, `requirements.txt`, `main.py`)은 이미 다 준비되어 있어요.

## 1단계 — GitHub에 코드 올리기 (Render는 GitHub 저장소를 보고 배포합니다)

이미 GitHub 계정이 있다는 가정하에:

1. https://github.com/new 에서 새 저장소 생성 (예: `finance-anl`), Public으로 설정
2. 로컬 터미널에서 다운로드한 파일들을 저장소 폴더에 넣고:
   ```bash
   git init
   git add .
   git commit -m "Finance.anl 초기 커밋"
   git branch -M main
   git remote add origin https://github.com/사용자명/finance-anl.git
   git push -u origin main
   ```
   저장소 최상위에는 이 구조가 되어야 합니다:
   ```
   finance-anl/
   ├── render.yaml          ← 저장소 루트에 있어야 Render가 자동 인식합니다
   ├── dashboard.html
   └── backend/
       ├── main.py
       ├── requirements.txt
       └── README.md
   ```

GitHub 웹사이트에서 "Add file → Upload files"로 드래그해서 올리셔도 됩니다 (터미널 없이 가능).

## 2단계 — Render에서 배포

1. https://render.com 접속 → GitHub 계정으로 가입/로그인
2. 대시보드에서 **New +** → **Blueprint** 클릭
3. 방금 만든 `finance-anl` 저장소 선택 → Render가 저장소 루트의 `render.yaml`을 자동으로 읽어서 서비스 설정을 채웁니다
4. **Apply** 클릭 → 자동으로 빌드 시작 (2~4분 소요)
5. 빌드가 끝나면 `https://finance-anl-backend.onrender.com` 같은 주소가 발급됩니다

배포 완료 확인: 발급된 주소 뒤에 `/api/health`를 붙여 접속해보세요.
예: `https://finance-anl-backend.onrender.com/api/health` → `{"status":"ok", ...}`가 뜨면 성공입니다.

## 3단계 — 프론트엔드와 연결

`dashboard.html`을 열어서 아래 줄을 실제 배포 주소로 바꿔주세요.

```js
const API_BASE = 'http://localhost:8000';
```
↓
```js
const API_BASE = 'https://finance-anl-backend.onrender.com';
```

바꾼 뒤 다시 저에게 업로드해주시면 이 값이 반영된 최신 `dashboard.html`을 다시 만들어드릴게요. 그 파일을 claude.ai에서 다시 열어 **공유(Share/Publish)** 버튼을 누르면, 그 링크를 받은 누구나 실제 살아있는 데이터로 대시보드를 볼 수 있습니다.

## 참고: 무료 플랜의 특성

- Render 무료 플랜은 15분 동안 요청이 없으면 서버가 슬립 모드로 들어갑니다. 슬립 상태에서 첫 요청이 오면 다시 깨어나는 데 30초~1분 정도 걸릴 수 있어요 (첫 로딩만 느리고 이후는 정상 속도).
- 포트폴리오 시연 직전에 `/api/health`를 한 번 미리 호출해서 깨워두면 면접관/채용담당자가 볼 때 느리지 않습니다.
- 완전히 상시 가동이 필요하면 Render의 유료 플랜(월 7달러부터)으로 업그레이드하면 슬립 없이 상시 동작합니다.
