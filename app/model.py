import json
import uuid
import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, NoResultFound

from .db import engine


class InvalidToken(Exception):
    """指定されたtokenが不正だったときに投げる"""


class SafeUser(BaseModel):
    """token を含まないUser"""

    id: int
    name: str
    leader_card_id: int

    class Config:
        orm_mode = True


def create_user(name: str, leader_card_id: int) -> str:
    """Create new user and returns their token"""
    with engine.begin() as conn:
        while True:
            token = str(uuid.uuid4())
            try:
                conn.execute(
                    text(
                        "INSERT INTO `user` (name, token, leader_card_id) "
                        "VALUES (:name, :token, :leader_card_id)"
                    ),
                    {"name": name, "token": token, "leader_card_id": leader_card_id},
                )
            except IntegrityError:
                continue
            break
    return token


def _get_user_by_token(conn, token: str) -> tuple[Optional[SafeUser], int]:
    res = conn.execute(
        text("SELECT id, name, leader_card_id, room_id FROM user WHERE token = :token"),
        {"token": token},
    )
    try:
        row = res.one()
    except NoResultFound:
        return None
    return (SafeUser(id=row.id, name=row.name, leader_card_id=row.leader_card_id), row.room_id)


def get_user_by_token(token: str) -> tuple[Optional[SafeUser], int]:
    with engine.begin() as conn:
        return _get_user_by_token(conn, token)


def _update_user(conn, token: str, name: str, leader_card_id: int) -> None:
    conn.execute(
        text(
            "UPDATE user SET name = :name, leader_card_id = :card "
            "WHERE token = :token"
        ),
        {"name": name, "card": leader_card_id, "token": token},
    )


def update_user(token: str, name: str, leader_card_id: int) -> None:
    with engine.begin() as conn:
        _update_user(conn, token, name, leader_card_id)


# 以下マルチプレイ用


class LiveDifficulty(Enum):
    normal = 1
    hard = 2


class JoinRoomResult(Enum):
    Ok = 1
    RoomFull = 2
    Disbanded = 3
    OtherError = 4


class WaitRoomStatus(Enum):
    Waiting = 1
    LiveStart = 2
    Dissolution = 3


class RoomInfo(BaseModel):
    room_id: int
    live_id: int
    joined_user_count: int
    max_user_count: int


class RoomUser(BaseModel):
    user_id: int
    name: str
    leader_card_id: int
    select_difficulty: LiveDifficulty
    is_me: bool
    is_host: bool


class ResultUser(BaseModel):
    user_id: int
    judge_count_list: list[int]
    score: int


def _create_room(conn, user: SafeUser, live_id: int, live_dif: LiveDifficulty) -> int:
    users = [
        {
            "id": user.id,
            "name": user.name,
            "leader_card_id": user.leader_card_id,
            "live_dif": live_dif.value,
        }
    ]
    users_json = json.dumps(users)
    time_now = datetime.datetime.now()
    result = conn.execute(
        text(
            "INSERT INTO `rooms` (live_id, hst_id, users, time_made) "
            "VALUES (:live_id, :hst_id, :users, :time_made)"
        ),
        {"live_id": live_id, "hst_id": user.id, "users": users_json, "time_made": time_now},
    )
    room_id = result.lastrowid
    conn.execute(
        text(
            "UPDATE user SET room_id = :room_id "
            "WHERE id = :user_id"
        ),
        {"room_id": room_id, "user_id": user.id},
    )
    return room_id


def create_room(token: str, live_id: int, live_dif: LiveDifficulty) -> int:
    with engine.begin() as conn:
        user = _get_user_by_token(conn, token)[0]
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        return _create_room(conn, user, live_id, live_dif)


def _room_list(conn, live_id: int) -> list[RoomInfo]:
    time_lim = datetime.datetime.now() - datetime.timedelta(minutes=5)
    execute_sent = (
        "SELECT room_id, live_id, j_usr_cnt, m_usr_cnt FROM rooms "
        "WHERE status = 1 AND time_made > :time_lim"
    )
    result = None
    if live_id == 0:
        result = conn.execute(
            text(execute_sent + " ORDER BY j_usr_cnt"),
            {"time_lim": time_lim}
        )
    else:
        result = conn.execute(
            text(execute_sent + " AND live_id = :live_id ORDER BY j_usr_cnt"),
            {"time_lim": time_lim, "live_id": live_id}
        )
    rows = result.all()
    room_infos = [
        RoomInfo(
            room_id=row.room_id,
            live_id=row.live_id,
            joined_user_count=row.j_usr_cnt,
            max_user_count=row.m_usr_cnt,
        )
        for row in rows
    ]
    return room_infos


def room_list(live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        return _room_list(conn, live_id)


def _room_join(
    conn, user: SafeUser, room_id: int, old_room_id: int, live_dif: LiveDifficulty
) -> JoinRoomResult:
    result = conn.execute(
        text(
            "SELECT status, j_usr_cnt, m_usr_cnt, users FROM rooms "
            "WHERE room_id = :room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    )
    try:
        row = result.one()
    except NoResultFound:
        return JoinRoomResult.OtherError
    j_usr_cnt = row.j_usr_cnt + 1
    users = json.loads(row.users)
    if room_id == old_room_id:
        for i, User in enumerate(users):
            if User["id"] == user.id:
                users.pop(i)
                j_usr_cnt -= 1
    elif row.j_usr_cnt == row.m_usr_cnt:
        return JoinRoomResult.RoomFull
    elif row.status == 3:
        return JoinRoomResult.Disbanded
    elif row.status != 1:
        return JoinRoomResult.OtherError
    users.append(
        {
            "id": user.id,
            "name": user.name,
            "leader_card_id": user.leader_card_id,
            "live_dif": live_dif.value,
        }
    )
    users_json = json.dumps(users)
    conn.execute(
        text(
            "UPDATE rooms SET j_usr_cnt = :j_usr_cnt, users = :users "
            "WHERE room_id = :room_id"
        ),
        {"j_usr_cnt": j_usr_cnt, "users": users_json, "room_id": room_id},
    )
    conn.execute(
        text(
            "UPDATE user SET room_id = :room_id "
            "WHERE id = :user_id"
        ),
        {"room_id": room_id, "user_id": user.id},
    )
    return JoinRoomResult.Ok


def room_join(token: str, room_id: int, live_dif: LiveDifficulty) -> JoinRoomResult:
    with engine.begin() as conn:
        user, old_room_id = _get_user_by_token(conn, token)
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        ret = _room_join(conn, user, room_id, old_room_id, live_dif)
        return ret


def _room_wait(
    conn, user: SafeUser, room_id: int
) -> tuple[WaitRoomStatus, list[RoomUser]]:
    result = conn.execute(
        text("SELECT status, hst_id, users, time_made FROM rooms WHERE room_id = :room_id"),
        {"room_id": room_id},
    )
    try:
        row = result.one()
    except NoResultFound:
        return (WaitRoomStatus.Dissolution, [])
    users = json.loads(row.users)
    time_now = datetime.datetime.now()
    if time_now - row.time_made >= datetime.timedelta(minutes=5):
        _room_start(conn, row.hst_id, room_id)
    room_user_list = [
        RoomUser(
            user_id=User["id"],
            name=User["name"],
            leader_card_id=User["leader_card_id"],
            select_difficulty=User["live_dif"],
            is_me=(user.id == User["id"]),
            is_host=(row.hst_id == User["id"]),
        )
        for User in users
    ]
    return (WaitRoomStatus(row.status), room_user_list)


def room_wait(token: str, room_id: int) -> tuple[WaitRoomStatus, list[RoomUser]]:
    with engine.begin() as conn:
        user = _get_user_by_token(conn, token)[0]
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        return _room_wait(conn, user, room_id)


def _room_start(conn, user_id: int, room_id: int) -> None:
    time_now = datetime.datetime.now()
    conn.execute(
        text(
            "UPDATE rooms SET status = :status, time_begin = :time_begin "
            "WHERE room_id = :room_id AND hst_id = :hst_id"
        ),
        {"status": 2, "time_begin": time_now,  "room_id": room_id, "hst_id": user_id},
    )
    return


def room_start(token: str, room_id: int) -> None:
    with engine.begin() as conn:
        user = _get_user_by_token(conn, token)[0]
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        _room_start(conn, user.id, room_id)
        return


def _room_end(
    conn, user: SafeUser, room_id: int, judge_count_list: list[int], score: int
) -> None:
    result = conn.execute(
        text(
            "SELECT j_usr_cnt, users, r_res_cnt FROM rooms "
            "WHERE room_id = :room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    )
    row = result.one()
    users = json.loads(row.users)
    r_res_cnt = row.r_res_cnt
    for i, User in enumerate(users):
        if User["id"] == user.id:
            User["judge_count_list"] = judge_count_list
            User["score"] = score
            users[i] = User
            r_res_cnt += 1
    users_json = json.dumps(users)
    conn.execute(
        text(
            "UPDATE rooms SET users = :users, r_res_cnt = :r_res_cnt "
            "WHERE room_id = :room_id"
        ),
        {"users": users_json, "r_res_cnt": r_res_cnt, "room_id": room_id},
    )
    if r_res_cnt == row.j_usr_cnt:
        conn.execute(
            text("UPDATE rooms SET status = 3 WHERE room_id = :room_id"),
            {"room_id": room_id},
        )
    return


def room_end(token: str, room_id: int, judge_count_list: list[int], score: int) -> None:
    with engine.begin() as conn:
        user = _get_user_by_token(conn, token)[0]
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        _room_end(conn, user, room_id, judge_count_list, score)
        return


def _room_result(conn, room_id: int) -> list[ResultUser]:
    result = conn.execute(
        text(
            "SELECT j_usr_cnt, r_res_cnt, time_begin, users FROM rooms "
            "WHERE room_id = :room_id"
        ),
        {"room_id": room_id},
    )
    row = result.one()
    r_res_cnt = row.r_res_cnt
    time_now = datetime.datetime.now()
    if time_now - row.time_begin >= datetime.timedelta(minutes=5):
        r_res_cnt = row.j_usr_cnt
    if r_res_cnt < row.j_usr_cnt:
        return []
    users = json.loads(row.users)
    res = [
        ResultUser(
            user_id=User["id"],
            judge_count_list=(User["judge_count_list"] if "judge_count_list" in User.keys() else [-1, -1, -1, -1, -1]),
            score=(User["score"] if "score" in User.keys() else -1),
        )
        for User in users
    ]
    return res


def room_result(room_id: int) -> list[ResultUser]:
    with engine.begin() as conn:
        return _room_result(conn, room_id)


def _room_remove(conn, room_id: int) -> None:
    conn.execute(
        text("DELETE FROM rooms WHERE room_id = :room_id"),
        {"room_id": room_id},
    )
    return


def _room_leave(conn, user: SafeUser, room_id: int) -> None:
    result = conn.execute(
        text(
            "SELECT j_usr_cnt, hst_id, users, r_res_cnt FROM rooms "
            "WHERE room_id = :room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    )
    row = result.one()
    j_usr_cnt = row.j_usr_cnt - 1
    if j_usr_cnt == 0:
        _room_remove(conn, room_id)
        return
    hst_id = row.hst_id
    users = json.loads(row.users)
    r_res_cnt = row.r_res_cnt
    is_room_mem = False
    for i, User in enumerate(users):
        if User["id"] == user.id:
            is_room_mem = True
            if "score" in User.keys():
                r_res_cnt -= 1
            else:
                users.pop(i)
            break
    if not is_room_mem:
        return
    if hst_id == user.id:
        hst_id = users[0]["id"]
    users_json = json.dumps(users)
    conn.execute(
        text(
            "UPDATE rooms SET j_usr_cnt = :j_usr_cnt, users = :users, "
            "hst_id = :hst_id, r_res_cnt = :r_res_cnt WHERE room_id = :room_id"
        ),
        {
            "j_usr_cnt": j_usr_cnt,
            "users": users_json,
            "hst_id": hst_id,
            "r_res_cnt": r_res_cnt,
            "room_id": room_id,
        },
    )
    return


def room_leave(token: str, room_id: int) -> None:
    with engine.begin() as conn:
        user = _get_user_by_token(conn, token)[0]
        if user is None:
            raise InvalidToken("指定されたtokenが不正です")
        _room_leave(conn, user, room_id)
        return
