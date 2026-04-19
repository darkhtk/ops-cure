# Ops-Cure

Ops-Cure는 Discord를 제어 인터페이스로 사용하고, 로컬 AI CLI를 실행 평면으로 사용하는 2-플레인 오케스트레이션 프레임워크입니다.

- `nas_bridge/`는 Ops-Cure Bridge입니다. Discord 연동, SQLite 상태 관리, 스레드 라우팅, 워커 등록, heartbeat, job, transcript를 담당하는 항상 켜져 있는 제어 평면입니다.
- `pc_launcher/`는 Ops-Cure Launcher입니다. Windows PC에서 YAML 프로젝트 설정을 읽고 bridge에 등록한 뒤, 에이전트별 worker를 실행하고 허용된 CLI adapter를 subprocess로 구동하는 실행 평면입니다.
- Discord `thread_id`가 세션 키이고, 로컬 CLI는 Discord 세부사항을 모른 채 불투명한 `session_id`만 받습니다.

## 구현 계획

1. NAS 쪽 bridge를 FastAPI, Discord slash command, SQLite 모델, 안전한 worker API로 구성합니다.
2. launcher가 프로젝트 manifest를 등록하도록 만들어 bridge가 미리 정의된 YAML 프로젝트만 허용하게 합니다.
3. Windows 쪽은 outbound polling만 사용해 launcher의 세션 claim과 worker의 job pull을 처리합니다.
4. Codex, Claude 같은 CLI는 고정 adapter 뒤에 감싸서 Discord 메시지가 raw shell command가 되지 않도록 합니다.
5. session, agent, job, transcript 상태를 SQLite에 저장하고, 그 라이프사이클을 Discord thread에 드러냅니다.
6. NAS용 Docker, PC용 sample YAML/prompt, 설치 문서, local development mode까지 함께 제공합니다.

## 저장소 구조

```text
repo/
  README.md
  nas_bridge/
    .env.example
    Dockerfile
    README.md
    docker-compose.yml
    requirements.txt
    data/
    app/
      __init__.py
      auth.py
      command_router.py
      config.py
      db.py
      discord_gateway.py
      main.py
      message_router.py
      models.py
      schemas.py
      session_service.py
      thread_manager.py
      transcript_service.py
      worker_registry.py
      api/
        __init__.py
        health.py
        sessions.py
        workers.py
  pc_launcher/
    .env.example
    README.md
    __init__.py
    bridge_client.py
    cli_adapters.py
    cli_worker.py
    config_loader.py
    launcher.py
    requirements.txt
    worker_runtime.py
    scripts/
      start_project.bat
    projects/
      sample_project/
        project.yaml
        prompts/
          coder.md
          finder.md
          planner.md
          reviewer.md
```

## SQLite 스키마

### `sessions`

- `id` TEXT PRIMARY KEY
- `project_name` TEXT NOT NULL
- `preset` TEXT NULL
- `discord_thread_id` TEXT UNIQUE NOT NULL
- `guild_id` TEXT NOT NULL
- `parent_channel_id` TEXT NOT NULL
- `workdir` TEXT NOT NULL
- `status` TEXT NOT NULL
- `created_by` TEXT NOT NULL
- `launcher_id` TEXT NULL
- `send_ready_message` BOOLEAN NOT NULL
- `created_at` TIMESTAMP NOT NULL
- `closed_at` TIMESTAMP NULL

### `agents`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `agent_name` TEXT NOT NULL
- `cli_type` TEXT NOT NULL
- `role` TEXT NOT NULL
- `is_default` BOOLEAN NOT NULL
- `status` TEXT NOT NULL
- `last_heartbeat_at` TIMESTAMP NULL
- `pid_hint` INTEGER NULL
- `worker_id` TEXT NULL
- `last_error` TEXT NULL

### `jobs`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `agent_name` TEXT NOT NULL
- `job_type` TEXT NOT NULL
- `source_discord_message_id` TEXT NULL
- `user_id` TEXT NOT NULL
- `input_text` TEXT NOT NULL
- `status` TEXT NOT NULL
- `worker_id` TEXT NULL
- `result_text` TEXT NULL
- `error_text` TEXT NULL
- `created_at` TIMESTAMP NOT NULL
- `claimed_at` TIMESTAMP NULL
- `completed_at` TIMESTAMP NULL

### `transcripts`

- `id` TEXT PRIMARY KEY
- `session_id` TEXT NOT NULL REFERENCES `sessions(id)`
- `direction` TEXT NOT NULL
- `actor` TEXT NOT NULL
- `content` TEXT NOT NULL
- `source_discord_message_id` TEXT NULL
- `created_at` TIMESTAMP NOT NULL

## 핵심 동작 흐름

1. Windows launcher가 `project.yaml` 파일들을 스캔해 bridge에 프로젝트 manifest를 등록합니다.
2. Discord 사용자가 `/project start name:<session-name> preset:<optional-preset>`를 실행합니다.
3. Bridge가 등록된 YAML manifest에서 preset을 해석하고, guild/channel/allowed user를 검증한 뒤, SQLite session row를 만들고 사용자가 지정한 이름으로 Discord thread를 엽니다.
4. Launcher가 pending launch를 claim하고, 설정된 agent마다 worker process를 하나씩 띄웁니다.
5. Worker는 bridge에 등록하고, heartbeat를 보내며, pending job을 pull합니다.
6. Thread 메시지는 `@agentname` prefix로 라우팅되며, 단일 agent 세션일 때만 자동 라우팅됩니다.
7. Worker 결과는 sanitize된 뒤 transcript에 저장되고, 동일한 Discord thread로 다시 게시됩니다.
