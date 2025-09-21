from flask import Flask, jsonify, request
import json
import os
from dotenv import load_dotenv

app = Flask(__name__)

# Загрузка переменных окружения
load_dotenv()
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions.json")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your_webhook_secret")  # Секрет для проверки

@app.route('/api/questions', methods=['GET'])
def get_questions():
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Фильтруем только одобренные и неаннулированные вопросы
        approved_questions = [
            {"id": q["id"], "username": q["username"], "question": q["question"], "answer": q["answer"]}
            for q in data["questions"]
            if q["status"] == "approved" and not q.get("cancelled", False)
        ]
        return jsonify({"questions": approved_questions})
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({"error": f"Ошибка чтения данных: {str(e)}"}), 500

@app.route('/api/update_questions', methods=['POST'])
def update_questions():
    # Проверка секрета
    auth_header = request.headers.get('Authorization')
    if auth_header != f"Bearer {WEBHOOK_SECRET}":
        return jsonify({"error": "Неверный секрет вебхука"}), 401

    try:
        new_data = request.json
        if not new_data or "questions" not in new_data:
            return jsonify({"error": "Неверный формат данных"}), 400

        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        return jsonify({"message": "Вопросы успешно обновлены"})
    except (json.JSONDecodeError, IOError) as e:
        return jsonify({"error": f"Ошибка записи данных: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))