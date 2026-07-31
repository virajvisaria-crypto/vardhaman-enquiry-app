# Vardhaman Enquiry Management System

Simple Flask-based enquiry capture and quote generation for Vardhaman Elastomer.

## Features

- Partner login (email/password)
- Add/edit customer enquiries  
- Auto-generate formatted quotes
- Order status tracking (advance/balance payment)
- All partners see all enquiries
- Mobile responsive

## Tech Stack

- Python Flask backend
- HTML/CSS/JavaScript frontend
- SQLite database
- Railway deployment

## Local Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run locally

```bash
python app.py
```

Visit `http://localhost:5000`

### 3. Create account

Click "Sign Up" and create an account with any email/password.

## Deployment to Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial Flask app"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/vardhaman-enquiry-app.git
git push -u origin main
```

### 2. Deploy on Railway

1. Go to railway.app
2. Click "New Project"
3. Select "Deploy from GitHub repo"
4. Find and select `vardhaman-enquiry-app`
5. Railway auto-detects Python and deploys
6. In Variables, set:
   - `SECRET_KEY=vardhaman-2024-secret` (change this)

That's it. Your app is live in ~2 minutes.

### 3. Test

Visit your Railway URL and sign up.

## Features

**Add Enquiry**
- Customer name, phone, email
- Product type, specs, qty, rate
- Delivery date
- Partner name (auto-filled with your name)

**Generate Quote**
- Auto-calculates 25% advance, 75% balance
- Copy formatted quote to clipboard
- Share via WhatsApp/email

**Track Orders**
- Toggle advance received
- Toggle balance received
- See payment status at a glance

## Database

SQLite database auto-creates on first run at `vardhaman.db`

Tables:
- `users` - login credentials
- `enquiries` - customer enquiries
- `orders` - payment tracking

## Notes

- All partners share the same login credentials
- Data is stored locally in SQLite (Railway persists it)
- No API keys needed, fully self-contained
- Enquiries are shared - all partners see all
