import json
import os
from datetime import date

import uvicorn
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
import sqlite3
from hashlib import sha256
import firebase_admin
from firebase_admin import credentials, messaging
from starlette.responses import JSONResponse

# Initialize FastAPI app
app = FastAPI()

# SQLite Database setup
DB_NAME = "chat_app.db"

# Firebase Admin SDK setup
# if not firebase_admin._apps:
#     cred = credentials.Certificate("firebase-service-account.json")
#     firebase_admin.initialize_app(cred)

if not firebase_admin._apps:
    firebase_cred = json.loads(os.getenv("FIREBASE_SERVICE_ACCOUNT"))
    cred = credentials.Certificate(firebase_cred)
    firebase_admin.initialize_app(cred)


# Create SQLite tables if they do not exist
def create_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """)
    cursor.executemany("""
        INSERT OR IGNORE INTO chat_rooms (id, name) VALUES (?, ?)
        """, [(1, "General"), (2, "Technology"), (3, "Random")])
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS push_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        sender_name TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(sender_id) REFERENCES users(id)
    )
    """)
    conn.commit()
    conn.close()


create_db()


# Models for input validation
class UserModel(BaseModel):
    username: str
    password: str


class PushTokenModel(BaseModel):
    user_id: int
    token: str


class MessageModel(BaseModel):
    sender_id: int
    sender_name: str
    message: str
    room_id: int


# Helper function for hashing passwords
def hash_password(password: str) -> str:
    return sha256(password.encode()).hexdigest()


# Unified response format
def unified_response(status: int, msg: str, data = None):
    return {"status": status, "msg": msg, "data": data}


# Routes
@app.post("/register")
async def register(user: UserModel):
    """Register a new user."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE username=?", (user.username,))
    existing_user = cursor.fetchone()
    if existing_user:
        return unified_response(1, "Username already exists")

    hashed_password = hash_password(user.password)
    cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (user.username, hashed_password))
    conn.commit()
    conn.close()

    return unified_response(0, "User registered successfully")


@app.post("/login")
async def login(user: UserModel):
    """Login an existing user."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM users WHERE username=?", (user.username,))
    existing_user = cursor.fetchone()
    if not existing_user or existing_user[2] != hash_password(user.password):
        return unified_response(1, "Invalid username or password")

    user_info = {
        "user_id": existing_user[0],
        "username": existing_user[1],
    }

    conn.close()
    return unified_response(0, "Login successful", user_info)


@app.post("/submit_push_token")
async def submit_push_token(push_token: PushTokenModel):
    """Store a push token in the database."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM push_tokens WHERE token=?", (push_token.token,))
    existing_token = cursor.fetchone()
    if existing_token:
        return unified_response(0, "Token already exists")

    cursor.execute("INSERT INTO push_tokens (user_id, token) VALUES (?, ?)", (push_token.user_id, push_token.token))
    conn.commit()
    conn.close()

    return unified_response(0, "Token stored successfully")


@app.post("/send_message_and_notify")
async def send_message_and_notify(message: MessageModel):
    """Store a new message in the database and send push notifications."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Verify room exists
    cursor.execute("SELECT * FROM chat_rooms WHERE id = ?", (message.room_id,))
    room = cursor.fetchone()
    if not room:
        return unified_response(1, "Invalid room ID")

    # Insert the message into the database
    cursor.execute(
        "INSERT INTO messages (sender_id, sender_name, message,room_id) VALUES (?, ?, ?, ?)",
        (message.sender_id, message.sender_name, message.message,message.room_id),
    )
    conn.commit()

    # Fetch all push tokens except the sender's
    # cursor.execute("SELECT token FROM push_tokens WHERE user_id != ?", (message.sender_id,))
    cursor.execute("SELECT token FROM push_tokens")
    tokens = cursor.fetchall()

    # Send FCM notifications using Firebase Admin SDK
    notifications = []
    for token in tokens:
        try:
            message_notification = messaging.Message(
                notification=messaging.Notification(
                    title=f"{room[1]}: {message.sender_name}",
                    body=message.message,
                ),
                token=token[0],
            )
            response = messaging.send(message_notification)
            notifications.append({"token": token[0], "response": response})
        except Exception as e:
            notifications.append({"token": token[0], "error": str(e)})

    conn.close()
    return unified_response(0, "Message sent and notifications processed", {"notifications": notifications})

@app.get("/get_chat_rooms")
async def get_chat_rooms():
    """Retrieve all chat rooms."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM chat_rooms ORDER BY id ASC")
    rooms = cursor.fetchall()
    conn.close()

    if not rooms:
        return unified_response(1, "No chat rooms found")

    formatted_rooms = [{"room_id": r[0], "name": r[1]} for r in rooms]
    return unified_response(0, "Chat rooms retrieved successfully", formatted_rooms)

@app.get("/get_messages/{room_id}")
async def get_messages(room_id: int):
    """Get messages for a specific chat room."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Verify room exists
    cursor.execute("SELECT * FROM chat_rooms WHERE id = ?", (room_id,))
    room = cursor.fetchone()
    if not room:
        return unified_response(1, "Invalid room ID")

    # Fetch messages for the room
    cursor.execute(
        "SELECT sender_id, sender_name, message, timestamp FROM messages WHERE room_id = ? ORDER BY timestamp ASC",
        (room_id,),
    )
    messages = cursor.fetchall()
    conn.close()

    formatted_messages = [
        {"sender_id": m[0], "sender_name": m[1], "message": m[2], "timestamp": m[3]}
        for m in messages
    ]

    print(json.dumps(unified_response(0, f"Messages retrieved successfully from {room[1]}", formatted_messages), indent=2))
    return unified_response(0, f"Messages retrieved successfully from {room[1]}", formatted_messages)

@app.get("/demo/")
async def get_demo(a: int = 0, b: int = 0):
    sum_result = a + b
    data = {"sum": sum_result, "date": date.today()}
    return JSONResponse(content=jsonable_encoder(data))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5001, reload=True)