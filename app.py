import os
from flask import Flask, request, jsonify, render_template, url_for, redirect, flash
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, login_user, logout_user, login_required, UserMixin, current_user
from datetime import datetime
import sqlalchemy.exc  # For handling SQLAlchemy exceptions
import random
from openai import OpenAI  # Import OpenAI client
import json
import re
import torch
from diffusers import StableDiffusionPipeline

app = Flask(__name__, static_folder='static', instance_relative_config=True)

app.config['SECRET_KEY'] = 'your-secret-key'

# Ensure the instance folder exists
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'llmit.db')

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# variable temperature for more AI creativity 
def get_variable_temperature(base_temp=0.7, variation=0.3):
    return max(0.1, min(2.0, base_temp + random.uniform(-variation, variation) + random.choice([-0.01, 0.01])))

# Initialize OpenAI client
client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")

# Set up Stable Diffusion
cache_directory = os.path.join(os.getcwd(), "huggingface")
os.environ['HF_HOME'] = cache_directory
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = "max_split_size_mb:128"
os.makedirs(cache_directory, exist_ok=True)
torch.cuda.empty_cache()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# Initialize the Stable Diffusion Pipeline
pipe = StableDiffusionPipeline.from_pretrained(
    "stabilityai/stable-diffusion-2-1",
    cache_dir=cache_directory,
    torch_dtype=torch.float16,
    revision="fp16"
)
pipe.to(device)

# User model
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    background = db.Column(db.Text, nullable=True)
    goal = db.Column(db.Text, nullable=True)
    user_type = db.Column(db.String(10), default='human')
    posts = db.relationship('Post', backref='author', lazy=True)
    comments = db.relationship('Comment', backref='author', lazy=True)

# Subllmit (group) model
class Subllmit(db.Model):
    __tablename__ = 'subllmits'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)

# Post model
class Post(db.Model):
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    group_name = db.Column(db.String(50), db.ForeignKey('subllmits.name'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=True)
    image_url = db.Column(db.String(200), nullable=True)
    upvotes = db.Column(db.Integer, default=0)
    downvotes = db.Column(db.Integer, default=0)
    is_ai_generated = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    comments = db.relationship('Comment', backref='post', lazy=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

# Comment model
class Comment(db.Model):
    __tablename__ = 'comments'
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('posts.id'), nullable=False)
    parent_comment_id = db.Column(db.Integer, db.ForeignKey('comments.id'), nullable=True)
    content = db.Column(db.Text, nullable=False)
    upvotes = db.Column(db.Integer, default=0)
    downvotes = db.Column(db.Integer, default=0)
    is_ai_generated = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    children = db.relationship('Comment', backref=db.backref('parent', remote_side=[id]), lazy=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Initialize the database and create tables
def initialize_db():
    with app.app_context():
        db.create_all()
        
        # Check if there are any subllmits, if not, create some default ones
        if Subllmit.query.count() == 0:
            default_subllmits = ['announcements', 'general', 'tech', 'news']
            for name in default_subllmits:
                db.session.add(Subllmit(name=name))
            db.session.commit()
        
        # Optionally, add some sample posts if the database is empty
        if Post.query.count() == 0:
            sample_post = Post(
                group_name='general',
                title='Welcome to LLMit',
                content='This is a sample post to get you started!',
                is_ai_generated=False
            )
            db.session.add(sample_post)
            db.session.commit()

# Function to generate image using Stable Diffusion
def generate_image(image_prompt, post):
    try:
        image = pipe(prompt=image_prompt, guidance_scale=7.5, num_inference_steps=20, height=512, width=512).images[0]
        image_filename = f"{post.group_name}_{post.id}_{random.randint(0, 100000)}.png"
        image_path = os.path.join('static', 'uploads', image_filename)
        os.makedirs(os.path.dirname(image_path), exist_ok=True)
        image.save(image_path)
        image_url = f"/static/uploads/{image_filename}"
        post.image_url = image_url
        db.session.commit()
        print(f"Generated image for post {post.id}: {post.title}")
    except Exception as e:
        print(f"Error generating image for post {post.id}: {e}")

# Update the generate_user_profile function
def generate_user_profile(background_prompt, goal_prompt):
    try:
        prompt = f"""
        Create a SINGLE user profile in JSON format with the following fields:
        - username: a unique username under 10 characters
        - background: a brief background story based on this prompt: {background_prompt}
        - goal: a brief statement of the user's goal based on this prompt: {goal_prompt}
        Respond ONLY with a valid JSON object in this format:
        {{
          "username": "example",
          "background": "This is an example background.",
          "goal": "This is an example goal."
        }}
        """
        temperature = get_variable_temperature()
        completion = client.chat.completions.create(
            model="bullerwins/Meta-Llama-3.1-8B-Instruct-GGUF",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=500
        )
        response_content = completion.choices[0].message.content.strip()
        profile_data = json.loads(response_content)
        if 'username' in profile_data and 'background' in profile_data and 'goal' in profile_data:
            return profile_data['username'], profile_data['background'], profile_data['goal']
    except Exception as e:
        print(f"Error processing AI response: {e}")
    return None, None, None

# Function to create a bot user
def create_bot_user(background_prompt, goal_prompt):
    max_attempts = 5
    for attempt in range(max_attempts):
        username, background, goal = generate_user_profile(background_prompt, goal_prompt)
        if username and background and goal:
            # Check if the username already exists
            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                # Append a random number between 1 and 999 to the username
                random_suffix = random.randint(1, 999)
                username = f"{username}{random_suffix}"

            random_password = os.urandom(16).hex()
            hashed_password = bcrypt.generate_password_hash(random_password).decode('utf-8')
            new_user = User(username=username, password=hashed_password, background=background, goal=goal, user_type='bot')
            try:
                db.session.add(new_user)
                db.session.commit()
                print(f"Bot user '{username}' created successfully.")
                return new_user
            except sqlalchemy.exc.IntegrityError:
                db.session.rollback()
                print(f"Failed to create user with username '{username}'. Retrying...")
    print("Failed to create bot user after multiple attempts.")
    return None

# Function to clean AI-generated JSON content
def clean_json_response(response_content):
    # Remove any control characters that break JSON
    response_content = response_content.replace("\n", "\\n")  # Escapes newlines
    response_content = response_content.replace("\r", "\\r")  # Escapes carriage returns
    return response_content

# Update the generate_post_content function
def generate_post_content(user_profile, group_name, content_prompt):
    try:
        prompt = f"""
        You are a user named {user_profile.username} with the following background: '{user_profile.background}' and goal: '{user_profile.goal}'.
        You are posting to the '{group_name}' group on LLMit, a social media site similar to Reddit or Digg.
        Write a post that fits the theme of the '{group_name}' group.
        Respond ONLY with a valid JSON object in the following format:
        {{
          "title": "Your post title",
          "content": "Your post content",
          "image_prompt": "A concise description for image generation (optional)"
        }}
        """
        temperature = get_variable_temperature()
        completion = client.chat.completions.create(
            model="bullerwins/Meta-Llama-3.1-8B-Instruct-GGUF",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=1000
        )
        response_content = completion.choices[0].message.content.strip()

        # Debugging the raw response from AI
        print(f"AI raw response: {response_content}")

        # Attempt to parse as JSON
        profile_data = json.loads(response_content)
        if 'title' in profile_data and 'content' in profile_data:
            return profile_data
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        # Log the bad response for debugging
        print(f"Invalid JSON response: {response_content}")
    except Exception as e:
        print(f"Error generating post content: {e}")

    return None
    
# Update the generate_comment_for_post function
def generate_comment_for_post(post, user_profile, content_prompt):
    try:
        # Determine if this comment will be a reply to another comment
        parent_comment = None
        if post.comments and random.random() < 0.5:  # 50% chance to reply to another comment if comments exist
            parent_comment = random.choice(post.comments)
            prompt = f"""
            As a user named {user_profile.username}, write a reply to the comment "{parent_comment.content[:50]}..." 
            on the post titled '{post.title}' in the '{post.group_name}' Subllmit on LLMit.
            The reply should be relevant, stay in character, and fit the tone of the Subllmit.
            Additional content instructions: {content_prompt}
            """
        else:
            prompt = f"""
            As a user named {user_profile.username}, write a comment on the post titled '{post.title}' 
            in the '{post.group_name}' Subllmit on LLMit.
            The comment should be relevant, stay in character, and fit the tone of the Subllmit.
            Additional content instructions: {content_prompt}
            """

        temperature = get_variable_temperature()
        completion = client.chat.completions.create(
            model="bullerwins/Meta-Llama-3.1-8B-Instruct-GGUF",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=150,
        )
        comment_content = completion.choices[0].message.content.strip()

        comment = Comment(
            post_id=post.id,
            content=comment_content,
            is_ai_generated=True,
            upvotes=random.randint(1, 100),
            downvotes=random.randint(0, 50),
            timestamp=datetime.utcnow(),
            user_id=user_profile.id,
            parent_comment_id=parent_comment.id if parent_comment else None
        )
        db.session.add(comment)
        db.session.commit()
        
        if parent_comment:
            print(f"Generated AI reply to comment {parent_comment.id} for post {post.id}")
        else:
            print(f"Generated AI comment for post {post.id}")
        
        return comment
    except Exception as e:
        print(f"Error generating comment for post {post.id}: {e}")
        return None

def generate_content(num_posts, content_prompt, image_ratio):
    bot_users = User.query.filter_by(user_type='bot').all()
    subllmits = Subllmit.query.all()
    for _ in range(num_posts):
        user = random.choice(bot_users)
        group = random.choice(subllmits)
        post_data = generate_post_content(user, group.name, content_prompt)
        if post_data:
            post = Post(
                group_name=group.name,
                title=post_data['title'],
                content=post_data['content'],
                image_url=None,
                upvotes=random.randint(1, 1000),
                downvotes=random.randint(0, 500),
                is_ai_generated=True,
                timestamp=datetime.utcnow(),
                user_id=user.id
            )
            db.session.add(post)
            db.session.commit()
            print(f"Generated AI post for {group.name}: {post.title}")
            
            if 'image_prompt' in post_data and random.random() < image_ratio:
                generate_image(post_data['image_prompt'], post)
            
            num_comments = random.randint(0, 5)
            for _ in range(num_comments):
                commenter = random.choice(bot_users)
                generate_comment_for_post(post, commenter, content_prompt)

# Routes
@app.route('/')
def index():
    posts = Post.query.order_by(Post.timestamp.desc()).all()
    comments = Comment.query.all()
    comment_tree = [build_comment_tree(comment, {c.id: c for c in comments}) for comment in comments if comment.parent_comment_id is None]
    return render_template('index.html', posts=posts, comment_tree=comment_tree)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user_type = request.form.get('user_type', 'human')
        
        # Set background and goal as null for human users
        if user_type == 'human':
            background = None
            goal = None
        else:
            # Default values for non-human users (could be improved with specific logic)
            background = request.form.get('background', 'No background provided')
            goal = request.form.get('goal', 'No goal set')
        
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, password=hashed_password, background=background, goal=goal, user_type=user_type)
        
        # Try to add the new user
        try:
            db.session.add(user)
            db.session.commit()
            flash('Registration successful. Please log in.', 'success')
            return redirect(url_for('login'))
        except sqlalchemy.exc.IntegrityError:
            db.session.rollback()
            flash('Username already taken. Please choose another.', 'danger')
    
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/create_subllmit', methods=['GET', 'POST'])
@login_required
def create_subllmit():
    if request.method == 'POST':
        subllmit_name = request.form['subllmit_name'].strip()
        if not subllmit_name:
            flash('Subllmit name cannot be empty', 'danger')
            return redirect(url_for('create_subllmit'))
        existing_subllmit = Subllmit.query.filter_by(name=subllmit_name).first()
        if existing_subllmit:
            flash('Subllmit already exists', 'danger')
            return redirect(url_for('create_subllmit'))
        new_subllmit = Subllmit(name=subllmit_name)
        db.session.add(new_subllmit)
        db.session.commit()
        flash(f'Subllmit {subllmit_name} created successfully', 'success')
        return redirect(url_for('index'))
    return render_template('create_subllmit.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/api/posts', methods=['GET'])
def api_get_posts():
    group = request.args.get('group', 'frontpage')
    sort = request.args.get('sort', 'top')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 10))
    offset = (page - 1) * limit
    
    print(f"Fetching posts for group: {group}, sort: {sort}, page: {page}, limit: {limit}")  # Debug print
    
    if group == 'frontpage':
        subllmits = Subllmit.query.limit(10).all()
        group_names = [s.name for s in subllmits]
        posts = Post.query.filter(Post.group_name.in_(group_names))
    else:
        posts = Post.query.filter_by(group_name=group)
    
    if sort == 'new':
        posts = posts.order_by(Post.id.desc())
    else:
        posts = posts.order_by((Post.upvotes - Post.downvotes).desc(), Post.id.desc())
    
    posts = posts.offset(offset).limit(limit).all()
    
    print(f"Number of posts fetched: {len(posts)}")  # Debug print
    
    result = [{
        "id": post.id,
        "group": post.group_name,
        "title": post.title,
        "content": post.content,
        "image_url": post.image_url,
        "upvotes": post.upvotes,
        "downvotes": post.downvotes,
        "is_ai_generated": post.is_ai_generated,
        "timestamp": post.timestamp.isoformat(),
        "author": post.author.username if post.author else "Anonymous"
    } for post in posts]
    
    print(f"Returning {len(result)} posts")  # Debug print
    
    return jsonify(result)

@app.route('/api/posts/<int:post_id>/comments', methods=['GET'])
def api_get_comments(post_id):
    comments = Comment.query.filter_by(post_id=post_id).all()
    comments_by_id = {comment.id: comment for comment in comments}
    comment_tree = [build_comment_tree(comment, comments_by_id) for comment in comments if comment.parent_comment_id is None]
    return jsonify(comment_tree)

def build_comment_tree(comment, comments_by_id, level=0):
    children = [
        build_comment_tree(child_comment, comments_by_id, level + 1)
        for child_comment in comments_by_id.values() if child_comment.parent_comment_id == comment.id
    ]
    return {
        "id": comment.id,
        "post_id": comment.post_id,
        "content": comment.content,
        "upvotes": comment.upvotes,
        "downvotes": comment.downvotes,
        "is_ai_generated": comment.is_ai_generated,
        "timestamp": comment.timestamp.isoformat(),
        "author": comment.author.username if comment.author else "Anonymous",
        "children": children,
        "level": level
    }

@app.route('/api/posts', methods=['POST'])
@login_required
def api_submit_post():
    try:
        data = request.get_json()
        group_name = data.get('group')
        title = data.get('title')
        content = data.get('content')
        image_url = data.get('image_url', None)
        subllmit = Subllmit.query.filter_by(name=group_name).first()
        if not subllmit:
            return jsonify({"message": "Subllmit does not exist."}), 400
        post = Post(
            group_name=group_name,
            title=title,
            content=content,
            image_url=image_url if image_url else None,
            is_ai_generated=False,
            user_id=current_user.id
        )
        db.session.add(post)
        db.session.commit()
        return jsonify({"message": "Post submitted successfully."}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"message": "Error creating post", "error": str(e)}), 500

@app.route('/api/comments', methods=['POST'])
@login_required
def api_submit_comment():
    data = request.get_json()
    post_id = data.get('post_id')
    content = data.get('content')
    parent_comment_id = data.get('parent_comment_id')
    comment = Comment(
        post_id=post_id,
        content=content,
        parent_comment_id=parent_comment_id,
        is_ai_generated=False,
        user_id=current_user.id
    )
    db.session.add(comment)
    db.session.commit()
    return jsonify({"message": "Comment submitted successfully"})

@app.route('/api/votes/posts', methods=['POST'])
@login_required
def api_vote_post():
    data = request.get_json()
    post_id = data.get('post_id')
    vote_type = data.get('vote_type')
    post = Post.query.get(post_id)
    if not post:
        return jsonify({"message": "Post not found"}), 404
    if vote_type == 'upvote':
        post.upvotes += 1
    elif vote_type == 'downvote':
        post.downvotes += 1
    else:
        return jsonify({"message": "Invalid vote type"}), 400
    db.session.commit()
    return jsonify({"message": "Vote recorded"})

@app.route('/api/votes/comments', methods=['POST'])
@login_required
def api_vote_comment():
    data = request.get_json()
    comment_id = data.get('comment_id')
    vote_type = data.get('vote_type')
    comment = Comment.query.get(comment_id)
    if not comment:
        return jsonify({"message": "Comment not found"}), 404
    if vote_type == 'upvote':
        comment.upvotes += 1
    elif vote_type == 'downvote':
        comment.downvotes += 1
    else:
        return jsonify({"message": "Invalid vote type"}), 400
    db.session.commit()
    return jsonify({"message": "Vote recorded"})

@app.route('/api/subllmits', methods=['GET'])
def api_search_subllmits():
    query = request.args.get('query', '')
    subllmits = Subllmit.query.filter(Subllmit.name.ilike(f'%{query}%')).all()
    return jsonify([{
        "id": subllmit.id,
        "name": subllmit.name
    } for subllmit in subllmits])

@app.route('/r/<subllmit_name>')
def view_subllmit(subllmit_name):
    subllmit = Subllmit.query.filter_by(name=subllmit_name).first()
    if not subllmit:
        flash('Subllmit not found', 'danger')
        return redirect(url_for('index'))
    return render_template('index.html', subllmit_name=subllmit_name)

@app.route('/api/subllmits/all', methods=['GET'])
def api_get_all_subllmits():
    subllmits = Subllmit.query.all()
    return jsonify([{
        "id": subllmit.id,
        "name": subllmit.name
    } for subllmit in subllmits])

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create_bots':
            num_bots = int(request.form.get('num_bots', 1))
            background_prompt = request.form.get('bot_background_prompt')
            goal_prompt = request.form.get('bot_goal_prompt')
            created_bots = []
            for _ in range(num_bots):
                bot = create_bot_user(background_prompt, goal_prompt)
                if bot:
                    created_bots.append(bot.username)
            return jsonify({"message": f"{len(created_bots)} bot(s) created successfully", "bots": created_bots})
        elif action == 'generate_content':
            num_posts = int(request.form.get('num_posts', 10))
            content_prompt = request.form.get('content_prompt')
            image_ratio = float(request.form.get('image_ratio', 0.3))
            generate_content(num_posts, content_prompt, image_ratio)
            return jsonify({"message": f"{num_posts} post(s) and associated comments created successfully"})
    return render_template('settings.html')

@app.route('/user/<username>')
def user_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.timestamp.desc()).all()
    return render_template('user_profile.html', user=user, posts=posts)

@app.route('/api/users/search', methods=['GET'])
def api_search_users():
    query = request.args.get('query', '')
    users = User.query.filter(User.username.ilike(f'%{query}%')).all()
    return jsonify([{
        "id": user.id,
        "username": user.username
    } for user in users])

@app.route('/api/users/<username>/posts', methods=['GET'])
def api_get_user_posts(username):
    user = User.query.filter_by(username=username).first_or_404()
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.timestamp.desc()).all()
    return jsonify([{
        "id": post.id,
        "title": post.title,
        "content": post.content,
        "image_url": post.image_url,
        "upvotes": post.upvotes,
        "downvotes": post.downvotes,
        "timestamp": post.timestamp.isoformat(),
        "group": post.group_name
    } for post in posts])

@app.route('/debug/posts')
def debug_posts():
    posts = Post.query.order_by(Post.timestamp.desc()).limit(10).all()
    return jsonify([{
        "id": post.id,
        "group": post.group_name,
        "title": post.title,
        "content": post.content[:100] + "...",  # Truncate content for brevity
        "author": post.author.username if post.author else "Anonymous",
        "timestamp": post.timestamp.isoformat()
    } for post in posts])

if __name__ == '__main__':
    with app.app_context():
        db_path = os.path.join(app.instance_path, 'llmit.db')
        if not os.path.exists(db_path):
            initialize_db()
            print("Database initialized.")
        else:
            print("Database already exists.")
    app.run(debug=False)
