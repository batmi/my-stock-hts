"""실행 중인 코드가 어느 커밋인가 — 기동 때 한 번 읽어 두고, 그 뒤 저장소가 바뀌었는지 견준다.

[왜 · 2026-11-14] 운영 기기(라즈베리파이)는 `git pull` 과 재기동이 따로 일어난다. pull 만 하고
 재기동하지 않으면 프로세스는 옛 코드로 계속 돌고, 재기동했더라도 '지금 도는 것이 어느 커밋인가'는
 기동 로그·하트비트·관제 화면 어디에도 없었다. 그래서 운용 점검 때마다 간접 증거로 추정했다
 (새 표가 DB 에 있는가, 로그 문구가 바뀌었는가, 기동 시각이 커밋보다 뒤인가).
 이 모듈은 그 한 가지 사실만 답한다.

[어떻게] `.git` 의 파일을 직접 읽는다(HEAD → refs/heads/<브랜치> 또는 packed-refs). git 명령을
 띄우지 않는다 — 하트비트·관제 화면이 주기적으로 부르는데, 하위 프로세스는 파이3 에서 비싸고
 터미널의 Ctrl+C 를 함께 받는다. 저장소가 아니거나(압축본으로 설치) 읽지 못하면 None 이다 —
 모르는 것을 지어내지 않는다.

[기동 때의 값] 이 모듈을 처음 import 한 순간의 커밋이다. main 이 기동 직후 로그를 남기며 불러
 오므로 사실상 '프로세스가 올린 코드'다. 파이썬은 이미 import 한 모듈을 다시 읽지 않으므로,
 그 뒤 저장소가 바뀌어도 도는 코드는 이 커밋이다(아직 import 하지 않은 모듈만 새 코드로 읽힐 수
 있다 — 그래서 바뀌었으면 재기동하라고 알린다).
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_dir(root):
    """저장소의 .git 디렉터리. 워크트리는 .git 이 'gitdir: <경로>' 한 줄짜리 파일이다."""
    p = os.path.join(root, ".git")
    if os.path.isdir(p):
        return p
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            head = f.read().strip()
        if head.startswith("gitdir:"):
            gd = head.split(":", 1)[1].strip()
            return gd if os.path.isabs(gd) else os.path.normpath(os.path.join(root, gd))
    return None


def read_head(root=ROOT):
    """저장소 HEAD 의 (커밋 40자, 브랜치). 모르는 값은 None — 분리된 HEAD 면 브랜치가 None 이다."""
    commit = branch = None
    try:
        gd = _git_dir(root)
        if not gd:
            return None, None
        with open(os.path.join(gd, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return (head or None), None
        ref = head.split(" ", 1)[1].strip()
        branch = ref.rsplit("/", 1)[-1]
        #  워크트리의 가지는 공용 저장소(commondir)에 있다.
        common = gd
        cd = os.path.join(gd, "commondir")
        if os.path.isfile(cd):
            with open(cd, encoding="utf-8") as f:
                common = os.path.normpath(os.path.join(gd, f.read().strip()))
        for base in (gd, common):
            p = os.path.join(base, ref)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    commit = f.read().strip() or None
                break
        if commit is None:
            packed = os.path.join(common, "packed-refs")
            if os.path.exists(packed):
                with open(packed, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith(("#", "^")) and line.endswith(" " + ref):
                            commit = line.split()[0]
    except Exception:           # noqa: BLE001 - 표시용 정보다. 읽지 못하면 모른다고 답한다.
        pass
    return commit, branch


_STARTED = read_head()          # 프로세스가 올린 코드 — 이 모듈을 처음 import 한 순간의 저장소 HEAD


def started():
    """기동 때의 (커밋, 브랜치)."""
    return _STARTED


def short(commit):
    return commit[:7] if commit else None


def label():
    """'4a23f94 (main)' — 화면·로그에 적는 한 토막. 모르면 '알 수 없음'."""
    commit, branch = _STARTED
    if not commit:
        return "알 수 없음"
    return f"{short(commit)} ({branch})" if branch else short(commit)


def drift():
    """기동 뒤 저장소 HEAD 가 바뀌었으면 지금 커밋(7자), 아니면 None.

    None 은 '같다' 또는 '모른다'다 — 기동 때나 지금 값을 읽지 못하면 바뀌었다고 말하지 않는다.
    """
    then = _STARTED[0]
    now = read_head()[0]
    if then and now and now != then:
        return short(now)
    return None
