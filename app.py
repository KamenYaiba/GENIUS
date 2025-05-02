from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import json
import os
import uuid
import google.generativeai as genai
from gtts import gTTS  # Google Text-to-Speech
from keys import GEMINI_KEY

app = Flask(__name__)
app.secret_key = "student_assessment_tool"  # For session management

# Configure Gemini API
genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel('gemini-2.0-flash')


@app.route('/', methods=['GET', 'POST'])
def index():
    """Instructor page to upload questions and answers"""
    if request.method == 'POST':
        # Get questions and answers from form
        try:
            questions_data = json.loads(request.form.get('questions_json', '{}'))
            answers_data = json.loads(request.form.get('answers_json', '{}'))

            # Store in session
            session['questions'] = questions_data
            session['answers'] = answers_data
            session['assessment_id'] = str(uuid.uuid4())

            return redirect(url_for('assessment'))
        except json.JSONDecodeError:
            error = "Invalid JSON format. Please check your input."
            return render_template('index.html', error=error)

    return render_template('index.html')


@app.route('/assessment', methods=['GET', 'POST'])
def assessment():
    """Student assessment page"""
    if 'questions' not in session:
        return redirect(url_for('index'))

    questions = session['questions']

    if request.method == 'POST':
        # Process student answers
        results = {}
        for q_id in range(len(questions)):
            student_answer = request.form.get(f'answer_{q_id}', '')

            # Check if student requested help
            if f'help_{q_id}' in request.form:
                # Student requested simpler language
                rephrased = rephrase_question(questions[q_id]['text'])
                # Return same page with rephrased question
                questions[q_id]['rephrased'] = rephrased
                return render_template('assessment.html', questions=questions, current_answers=request.form)

            # Store answer
            results[str(q_id)] = {
                'question': questions[q_id]['text'],
                'student_answer': student_answer,
            }

        if results:
            # Evaluate all answers
            for q_id in results:
                answer_key = session['answers'][q_id]
                question = questions[int(q_id)]
                results[q_id]['evaluation'] = evaluate_answer(
                    results[q_id]['student_answer'],
                    answer_key['correct_answer'],
                    question['type'],
                    answer_key['points']
                )

            # Store results in session
            session['results'] = results
            return redirect(url_for('results'))

    return render_template('assessment.html', questions=questions)


@app.route('/results', methods=['GET', 'POST'])
def results():
    """Results page for instructor review"""
    if 'results' not in session:
        return redirect(url_for('index'))

    results = session['results']

    if request.method == 'POST':
        # Instructor updated grades
        for q_id in results:
            new_score = request.form.get(f'score_{q_id}')
            if new_score and new_score.isdigit():
                results[q_id]['evaluation']['score'] = int(new_score)

        session['results'] = results
        return redirect(url_for('results'))

    # Calculate total score
    total_score = sum(item['evaluation']['score'] for item in results.values())
    max_score = sum(item['evaluation']['max_score'] for item in results.values())

    return render_template('results.html', results=results,
                           total_score=total_score, max_score=max_score)


@app.route('/text-to-speech', methods=['POST'])
def text_to_speech():
    """Generate speech from text"""
    data = request.get_json()
    text = data.get('text', '')

    if not text:
        return jsonify({'error': 'No text provided'}), 400

    try:
        # Generate speech using gTTS
        tts = gTTS(text=text, lang='en', slow=False)

        # Save to a temporary file
        temp_filename = f"temp_{uuid.uuid4()}.mp3"
        temp_path = os.path.join("static", "audio", temp_filename)

        # Ensure the directory exists
        os.makedirs(os.path.dirname(temp_path), exist_ok=True)

        tts.save(temp_path)

        # Return the audio file URL
        audio_url = url_for('static', filename=f'audio/{temp_filename}')
        return jsonify({'audio_url': audio_url})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/process-speech', methods=['POST'])
def process_speech():
    """Process speech data from the client"""
    data = request.get_json()
    question_id = data.get('question_id')
    speech_text = data.get('speech_text', '')

    if not speech_text:
        return jsonify({'error': 'No speech data provided'}), 400

    return jsonify({'success': True, 'text': speech_text})


def rephrase_question(question_text):
    """Use Gemini to rephrase a question for better understanding"""
    prompt = f"""
    You are helping a teacher explain a concept to a young student (grades 1-3).
    The student didn't understand this question: "{question_text}"

    Please rephrase the question to be more accessible to a young child without
    changing what concept is being tested. Use simple words and short sentences.
    Give ONLY the rephrased question with no explanations or other text.
    """

    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return f"Simpler version: {question_text}"


def evaluate_answer(student_answer, correct_answer, question_type, total_points):
    """Use Gemini to evaluate student answer"""

    if question_type == "multiple_choice":
        # Simple comparison for multiple choice
        is_correct = student_answer.lower() == correct_answer.lower()
        score = total_points if is_correct else 0
        feedback = "Correct!" if is_correct else f"The correct answer was: {correct_answer}"

        return {
            "score": score,
            "max_score": total_points,
            "feedback": feedback
        }

    # For other question types, use Gemini
    prompt = f"""
    You are evaluating a young student's answer (grades 1-3).
    Focus on understanding, not language skills.

    Question type: {question_type}
    Correct answer: {correct_answer}
    Student answer: {student_answer}
    Maximum points: {total_points}

    Evaluate the student's conceptual understanding only, ignore spelling and grammar.
    Give a score from 0 to {total_points} and brief, encouraging feedback.

    Format your response exactly like this:
    SCORE: [number]
    FEEDBACK: [1-2 sentences of feedback]
    """

    try:
        response = gemini_model.generate_content(prompt)
        result = response.text.strip()

        # Extract score and feedback
        score_line = [line for line in result.split('\n') if line.startswith('SCORE:')]
        feedback_line = [line for line in result.split('\n') if line.startswith('FEEDBACK:')]

        if score_line and feedback_line:
            try:
                score = int(score_line[0].replace('SCORE:', '').strip())
                feedback = feedback_line[0].replace('FEEDBACK:', '').strip()
            except ValueError:
                score = 0
                feedback = "Error processing score. Please review manually."
        else:
            score = 0
            feedback = "Error parsing AI evaluation. Please review manually."

        return {
            "score": score,
            "max_score": total_points,
            "feedback": feedback
        }
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return {
            "score": 0,
            "max_score": total_points,
            "feedback": f"Error evaluating answer: {e}"
        }


if __name__ == '__main__':
    app.run(debug=True)