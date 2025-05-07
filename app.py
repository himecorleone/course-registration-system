import os
import json
import subprocess
import threading
import time
import sys
import logging
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import schedule
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash

# =====================================================================
# Course Registration System
# =====================================================================

# Course day and time mapping
course_day_time_mapping = {
    "051001":  {"day_index": 2, "start_time": "18:00"},
    "051002":  {"day_index": 4, "start_time": "16:30"},
    "051003":  {"day_index": 6, "start_time": "15:15"},
    "051011":  {"day_index": 1, "start_time": "21:00"},
    "051012":  {"day_index": 4, "start_time": "18:00"},
    "0510011": {"day_index": 2, "start_time": "18:30"}
}

# Course day mapping for logging in English
course_day_mapping = {
    "051001":  "Wednesday 18:00-19:30",
    "051002":  "Friday 16:30-18:00",
    "051003":  "Sunday 15:15-16:45",
    "051011":  "Tuesday 21:00-22:30",
    "051012":  "Friday 18:00-19:30",
    "0510011": "Wednesday 18:30-20:30"
}

def setup_logging(credentials_file, log_dir="/app/data/logs"):
    """Set up logging configuration."""
    os.makedirs(log_dir, exist_ok=True)
    
    file_name = os.path.basename(credentials_file)
    handlers = [
        logging.FileHandler(f"{log_dir}/course_scheduler.{file_name}.log"),
        logging.StreamHandler()
    ]
    format_str = f'[{file_name}] %(asctime)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=format_str, handlers=handlers)
    
    return logging.getLogger(__name__)

def calculate_next_course_time(course_number, current_time):
    """Calculate the next occurrence of a course from the current time."""
    course_info = course_day_time_mapping.get(course_number)
    if not course_info:
        return None

    # Extract the day index and start time
    course_day_index = course_info["day_index"]
    start_time_str = course_info["start_time"]
    start_time = datetime.strptime(start_time_str, "%H:%M")

    # Combine current date with course start time
    course_start_datetime = current_time.replace(
        hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0
    )

    # Adjust to match the course day
    days_difference = (course_day_index - current_time.weekday()) % 7
    if days_difference == 0 and current_time.time() > start_time.time():
        # If today is the course day but the course time has passed, schedule for next week
        days_difference = 7
    
    course_start_datetime += timedelta(days=days_difference)
    
    return course_start_datetime

def get_registration_time(course_start_time):
    """Calculate the time to attempt registration (7 minutes before course start)."""
    return course_start_time - timedelta(minutes=7)

def course_has_just_started(course_number, current_time):
    """Check if a course has just started (within 40 minutes)."""
    course_info = course_day_time_mapping.get(course_number)
    if not course_info:
        return False

    # Extract the day index and start time
    course_day_index = course_info["day_index"]
    start_time_str = course_info["start_time"]
    start_time = datetime.strptime(start_time_str, "%H:%M")

    # Combine current date with course start time
    course_start_datetime = current_time.replace(
        hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0
    )

    # Adjust to match the course day
    days_difference = (course_day_index - current_time.weekday()) % 7
    course_start_datetime += timedelta(days=days_difference)

    # Check if the current time is within 40 minutes of the course start time
    return abs((current_time - course_start_datetime).total_seconds()) <= 2500

def read_credentials_file(credentials_file, logger, current_time):
    """Read credentials and course information from file."""
    try:
        with open(credentials_file, 'r') as file:
            lines = file.readlines()
            if len(lines) < 2:
                logger.error(f"Error: The file '{credentials_file}' must contain at least email and password.")
                sys.exit(1)
                
            email = lines[0].strip()  # Read the first line (email)
            password = lines[1].strip()  # Read the second line (password)

            # Read or initialize the third line
            if len(lines) >= 3 and lines[2].strip():
                all_days = [day.strip() for day in lines[2].split(",")]
                # Separate eligible courses and "do not sign up" courses
                existing_days = {day for day in all_days if not day.startswith("!")}
                excluded_days = {day[1:] for day in all_days if day.startswith("!")}
            else:
                existing_days = set()
                excluded_days = set()
                if len(lines) < 3:
                    lines.append("\n")  # Add an empty line if missing

            # Remove courses that just started from the `existing_days` set
            courses_to_remove = {course for course in existing_days if course_has_just_started(course, current_time)}
            if courses_to_remove:
                logger.info(f"Removing courses that just started: {courses_to_remove}")
                existing_days -= courses_to_remove

                # Update the third line in the credentials file
                all_days_combined = sorted(existing_days) + [f"!{day}" for day in sorted(excluded_days)]
                lines[2] = ", ".join(all_days_combined) + "\n"
                with open(credentials_file, 'w') as file:
                    file.writelines(lines)

            return email, password, existing_days, excluded_days, lines

    except FileNotFoundError:
        logger.error(f"Error: The file '{credentials_file}' was not found.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error reading the file: {str(e)}")
        sys.exit(1)

def setup_webdriver(logger):
    """Set up and configure the Firefox WebDriver with minimal resources."""
    # Firefox headless options - simplified for cloud environment
    firefox_options = webdriver.FirefoxOptions()
    firefox_options.add_argument("--headless")
    firefox_options.add_argument("--no-sandbox")
    firefox_options.add_argument("--disable-dev-shm-usage")
    firefox_options.add_argument("--disable-gpu")
    firefox_options.add_argument("--window-size=1280,1696")
    
    # Use installed geckodriver or the one in PATH
    service = None
    
    # Check common locations for geckodriver
    driver_paths = [
        "/usr/local/bin/geckodriver",
        "/usr/bin/geckodriver",
        "/app/bin/geckodriver"
    ]
    
    for path in driver_paths:
        if os.path.exists(path):
            service = Service(path)
            logger.info(f"Using geckodriver from: {path}")
            break
    
    # If not found, rely on PATH
    if service is None:
        logger.info("Using geckodriver from PATH")
        service = Service()
    
    # Set environment variables for Firefox
    os.environ['MOZ_HEADLESS'] = '1'
    
    # Try multiple times with progressive sleep
    for attempt in range(3):
        try:
            driver = webdriver.Firefox(service=service, options=firefox_options)
            driver.set_page_load_timeout(300)  # Increased to 300 seconds (5 minutes)
            return driver
        except Exception as e:
            if attempt < 2:  # Don't sleep on the last attempt
                sleep_time = (attempt + 1) * 2
                logger.warning(f"WebDriver initialization failed (attempt {attempt+1}/3). Retrying in {sleep_time}s. Error: {str(e)}")
                time.sleep(sleep_time)
            else:
                logger.error(f"WebDriver initialization failed after 3 attempts: {str(e)}")
                raise

def check_and_register_courses(credentials_file):
    """Main function to check for vacancies and register for courses."""
    # Initialize time
    current_time = datetime.now()
    start_time = time.time()
    max_runtime = 900  # Set maximum runtime to 15 minutes (increased for longer timeouts)
    
    # Setup logging
    logger = setup_logging(credentials_file)
    logger.info(f"Script started at: {current_time.strftime('%Y-%m-%d %H:%M:%S')} with file {credentials_file}")
    
    # Read credentials
    email, password, existing_days, excluded_days, lines = read_credentials_file(credentials_file, logger, current_time)
    
    try:
        # Initialize WebDriver
        driver = setup_webdriver(logger)
        
        def check_timeout():
            if time.time() - start_time > max_runtime:
                logger.warning("Operation taking too long, terminating early")
                raise TimeoutError("Script runtime exceeded maximum allowed time")
        
        try:
            # Navigate to the course booking page
            booking_url = "https://buchung.hochschulsport-hamburg.de/angebote/Sommersemester_2025/_Badminton.html"
            logger.info(f"Navigating to {booking_url}")
            driver.get(booking_url)
            original_window = driver.current_window_handle
            
            # Wait for page to load completely
            logger.info("Waiting for page to load")
            time.sleep(4)
            
            # Check timeout after initial page load
            check_timeout()
            
            # Get list of all available courses
            courses = driver.find_elements(By.CLASS_NAME, 'bs_btn_vormerkliste')
            logger.info(f'Found {len(courses)} course(s) available for registration')
            
            # Collect available course numbers
            available_days = []
            for course in courses:
                # Find the parent <tr> element that contains the button
                try:
                    parent_row = course.find_element(By.XPATH, "./ancestor::tr")
                    # Find the <td> with the class 'bs_sknr' inside this row
                    coursenr = parent_row.find_element(By.CLASS_NAME, "bs_sknr").text
                    logger.info(f'Found course: {course_day_mapping.get(coursenr, "Unknown course")}')
                    available_days.append(coursenr)
                except Exception as e:
                    logger.warning(f"Error getting course number: {str(e)}")
            
            # Process each course
            signed_up_days = set()
            
            for idx, coursenr in enumerate(available_days):
                # Check timeout before processing each course
                check_timeout()
                
                if coursenr in excluded_days:
                    logger.info(f"Skipping: {course_day_mapping.get(coursenr)} (marked as excluded)")
                    continue
                    
                if coursenr in existing_days:
                    logger.info(f"Skipping: {course_day_mapping.get(coursenr)} (already in configuration)")
                    continue
                
                try:
                    # Get the current course button (may have been refreshed)
                    updated_courses = driver.find_elements(By.CLASS_NAME, 'bs_btn_vormerkliste')
                    if idx >= len(updated_courses):
                        logger.error(f"Course index {idx} is out of bounds. Total courses: {len(updated_courses)}")
                        continue
                        
                    # Click on the course to register
                    logger.info(f"Attempting to register for: {course_day_mapping.get(coursenr)}")
                    updated_courses[idx].click()
                    
                    # Wait and switch to the new window
                    time.sleep(4)
                    window_handles = driver.window_handles
                    new_window = None
                    
                    for handle in window_handles:
                        if handle != original_window:
                            new_window = handle
                            driver.switch_to.window(new_window)
                            break
                    
                    if not new_window:
                        logger.error("Failed to open new window")
                        continue
                    
                    # Look for booking button
                    try:
                        wait = WebDriverWait(driver, 10)
                        logger.info("Looking for booking button")
                        booking_button = wait.until(EC.presence_of_element_located((By.XPATH, '//input[@value="buchen"]')))
                        booking_button.click()
                        logger.info("Clicked booking button")
                        
                    except Exception as e:
                        logger.error(f"No booking available: {str(e)}")
                        driver.close()
                        driver.switch_to.window(original_window)
                        continue
                    
                    # Open up login fields
                    logger.info("Opening login form")
                    wait.until(EC.presence_of_element_located((By.ID, "bs_pw_anmlink"))).click()
                    
                    # Input login credentials
                    logger.info("Entering login credentials")
                    email_field = wait.until(EC.presence_of_element_located((By.NAME, "pw_email")))
                    email_field.send_keys(email)
                    
                    password_field = wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="password"]')))
                    password_field.send_keys(password)
                    
                    # Submit login form
                    logger.info("Submitting login form")
                    password_field.send_keys(Keys.RETURN)
                    
                    # Wait for login to complete
                    logger.info("Waiting for login to complete")
                    time.sleep(10)  # Keep the longer wait time from the original
                    
                    # Check timeout before proceeding with registration
                    check_timeout()
                    
                    # Check the terms checkbox
                    try:
                        logger.info("Checking terms checkbox")
                        checkbox = wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="checkbox"]')))
                        driver.execute_script("arguments[0].checked = true;", checkbox)
                        
                        # Submit registration
                        logger.info("Submitting registration")
                        submit_button = wait.until(EC.presence_of_element_located((By.ID, 'bs_submit')))
                        submit_button.click()
                        time.sleep(10)  # Keep the longer wait time from the original
                        
                        # Confirm registration
                        logger.info("Confirming registration")
                        confirm_button = wait.until(EC.presence_of_element_located((By.XPATH, '//input[@type="submit"]')))
                        confirm_button.click()
                        time.sleep(5)  # Keep the 5 second wait time from the original
                        
                        # Check result
                        if driver.title == 'Bestätigung':
                            logger.info(f'Successfully registered for {course_day_mapping.get(coursenr)}')
                            signed_up_days.add(coursenr)
                        elif 'Ihre Buchung konnte nicht ausgeführt werden.' in driver.page_source:
                            logger.info('You are already registered for this course')
                            signed_up_days.add(coursenr)
                        else:
                            logger.error(f'Unknown registration status: {driver.title}')
                            # Log the page source for debugging
                            logger.debug(f"Page source: {driver.page_source[:1000]}...")
                            
                    except Exception as e:
                        logger.error(f"Error during registration process: {str(e)}")
                    
                except Exception as e:
                    logger.error(f"Error processing course {coursenr}: {str(e)}")
                    
                finally:
                    # Close the new window and switch back
                    if driver.current_window_handle != original_window:
                        driver.close()
                        driver.switch_to.window(original_window)
            
            # Update the credentials file with newly registered courses
            all_days = existing_days.union(signed_up_days)
            all_days_combined = sorted(all_days) + [f"!{day}" for day in sorted(excluded_days)]
            lines[2] = ", ".join(all_days_combined) + "\n"
            
            try:
                with open(credentials_file, 'w') as file:
                    file.writelines(lines)
                    logger.info(f"Updated file with days: {lines[2].strip()}")
            except Exception as e:
                logger.error(f"Error writing to the file: {str(e)}")
                
        except TimeoutError as te:
            logger.error(f"Script timed out: {str(te)}")
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            
    except Exception as e:
        logger.error(f"Error setting up WebDriver: {str(e)}")
    finally:
        # Clean up
        if 'driver' in locals():
            driver.quit()
        logger.info(f"Script completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"Total runtime: {time.time() - start_time:.2f} seconds")

def schedule_course_registrations(credentials_file, logger):
    """Schedule registration attempts for all courses 7 minutes before they start."""
    current_time = datetime.now()
    
    # Clear any existing schedules
    schedule.clear()
    
    # Get all courses from mapping
    for course_number, course_info in course_day_time_mapping.items():
        # Calculate next occurrence of this course
        next_course_time = calculate_next_course_time(course_number, current_time)
        if not next_course_time:
            continue
            
        # Calculate time to register (7 minutes before course start)
        registration_time = get_registration_time(next_course_time)
        
        # If registration time is in the past, skip it
        if registration_time <= current_time:
            continue
            
        # Schedule registration attempt
        time_diff = (registration_time - current_time).total_seconds()
        time_diff_hours = time_diff / 3600
        
        logger.info(f"Scheduling registration for {course_day_mapping.get(course_number)} at {registration_time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"({time_diff_hours:.2f} hours from now)")
        
        # Schedule the job
        job = schedule.every().day.at(registration_time.strftime("%H:%M")).do(
            check_and_register_courses, credentials_file=credentials_file
        )
        job.tag(f"course_{course_number}")

def run_registration_scheduler(credentials_file):
    """Run the registration scheduler continuously."""
    # Initialize time
    current_time = datetime.now()
    
    # Setup logging
    logger = setup_logging(credentials_file)
    logger.info(f"Scheduler started at: {current_time.strftime('%Y-%m-%d %H:%M:%S')} with file {credentials_file}")
    
    # Initial run to check for any currently available registrations
    check_and_register_courses(credentials_file)
    
    # Schedule future registration attempts
    schedule_course_registrations(credentials_file, logger)
    
    # Function to regularly update the schedule (once per day)
    def update_schedule():
        logger.info("Updating course registration schedule")
        schedule_course_registrations(credentials_file, logger)
    
    # Schedule daily updates to the registration schedule
    schedule.every().day.at("00:05").do(update_schedule)
    
    # Run the scheduler indefinitely
    logger.info("Scheduler running.")
    while True:
        schedule.run_pending()
        time.sleep(1)

# =====================================================================
# Web Interface
# =====================================================================

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'development-key')

# Set up logging configuration for Flask app
app_logger = logging.getLogger("app")
app_logger.setLevel(logging.INFO)

# Create log directory if it doesn't exist
os.makedirs("/app/data/logs", exist_ok=True)

# Add handlers
app_logger.addHandler(logging.FileHandler("/app/data/logs/app.log"))
app_logger.addHandler(logging.StreamHandler())
app_logger.propagate = False

# Start the schedulers in background threads
def run_schedulers():
    """Start registration schedulers for all accounts."""
    # Create directories if they don't exist
    os.makedirs('/app/data/credentials', exist_ok=True)
    os.makedirs('/app/data/logs', exist_ok=True)
    
    # Load accounts from environment variable or file
    accounts = load_accounts()
    app_logger.info(f"Starting schedulers for {len(accounts)} accounts")
    
    # Create credential files and start schedulers
    for i, account in enumerate(accounts):
        # Create credential file
        credential_path = f'/app/data/credentials/user{i}.txt'
        with open(credential_path, 'w') as f:
            f.write(f"{account['email']}\n")
            f.write(f"{account['password']}\n")
            f.write(f"{account.get('courses', '')}\n")
        
        # Start scheduler thread for this account
        scheduler_thread = threading.Thread(
            target=run_registration_scheduler,
            args=(credential_path,),
            daemon=True,
            name=f"scheduler_{i}"
        )
        scheduler_thread.start()
        app_logger.info(f"Started scheduler thread for account {i}: {account['email']}")

# Routes for web interface
@app.route('/')
def dashboard():
    """Main dashboard view"""
    accounts = load_accounts()
    course_status = get_course_status()
    
    # Get next scheduled registrations
    next_registrations = get_next_registrations()
    
    # Get recent log entries for the dashboard
    log_entries = get_log_entries()[:10]  # Limit to the 10 most recent entries
    
    # Check scheduler health
    scheduler_healthy = check_scheduler_health()
    if not scheduler_healthy:
        flash('Warning: Some scheduler processes may not be running correctly. Check the logs or restart the service.', 'warning')
    
    return render_template('dashboard.html', 
                          accounts=accounts, 
                          course_status=course_status,
                          next_registrations=next_registrations,
                          log_entries=log_entries,
                          scheduler_healthy=scheduler_healthy)

@app.route('/accounts', methods=['GET', 'POST'])
def manage_accounts():
    """Account management page"""
    if request.method == 'POST':
        # Handle form submission for adding/editing account
        email = request.form.get('email')
        password = request.form.get('password')
        excluded_courses = request.form.getlist('excluded_courses')
        
        app_logger.info(f"Processing account update/creation for: {email}")
        
        # Update accounts list
        accounts = load_accounts()
        
        # Check if updating existing or adding new
        account_idx = request.form.get('account_idx')
        if account_idx and account_idx.isdigit():
            # Update existing
            idx = int(account_idx)
            if idx < len(accounts):
                accounts[idx]['email'] = email
                accounts[idx]['password'] = password
                accounts[idx]['courses'] = ','.join(['!' + c for c in excluded_courses])
                app_logger.info(f"Updated existing account: {email}")
        else:
            # Add new
            accounts.append({
                'email': email,
                'password': password,
                'courses': ','.join(['!' + c for c in excluded_courses])
            })
            app_logger.info(f"Added new account: {email}")
            
        # Save and restart schedulers
        if save_accounts(accounts):
            flash('Account saved successfully! Schedulers restarted.', 'success')
        else:
            flash('Error saving account. Check logs for details.', 'danger')
            
        return redirect(url_for('dashboard'))
        
    # GET request - show accounts page
    accounts = load_accounts()
    all_courses = get_all_courses()
    return render_template('accounts.html', 
                          accounts=accounts,
                          all_courses=all_courses)

@app.route('/accounts/delete/<int:idx>', methods=['POST'])
def delete_account(idx):
    """Delete an account"""
    accounts = load_accounts()
    if idx < len(accounts):
        deleted_email = accounts[idx]['email']
        del accounts[idx]
        app_logger.info(f"Deleted account: {deleted_email}")
        
        if save_accounts(accounts):
            flash('Account deleted successfully! Schedulers restarted.', 'success')
        else:
            flash('Error deleting account. Check logs for details.', 'danger')
    else:
        flash('Account not found!', 'danger')
        
    return redirect(url_for('manage_accounts'))

@app.route('/status')
def status_page():
    """Detailed status page"""
    log_entries = get_log_entries()
    accounts = load_accounts()
    
    # Get recent application logs
    app_logs = get_recent_logs('/app/data/logs/app.log', 50)
    
    # Get next registrations
    next_registrations = get_next_registrations()
    
    # Scheduler health check
    scheduler_health = check_scheduler_health()
    active_threads = get_active_threads()
    
    return render_template('status.html', 
                          log_entries=log_entries, 
                          accounts=accounts,
                          app_logs=app_logs,
                          next_registrations=next_registrations,
                          scheduler_health=scheduler_health,
                          active_threads=active_threads)

@app.route('/run-test')
def run_test():
    """Endpoint to manually trigger course registration checks for one account"""
    accounts = load_accounts()
    if not accounts:
        return jsonify({"status": "error", "message": "No accounts configured"})
    
    # Just use the first account for testing
    account = accounts[0]
    credential_path = f'/app/data/credentials/user0.txt'
    
    # Create a test thread to run the check
    test_thread = threading.Thread(
        target=check_and_register_courses,
        args=(credential_path,),
        daemon=True,
        name="test_thread"
    )
    test_thread.start()
    
    return jsonify({
        "status": "started",
        "message": f"Test run for account {account['email']} started. Check logs for results."
    })

@app.route('/health')
def health_check():
    return jsonify({"status": "healthy"})

# Helper functions
def load_accounts():
    """Load accounts from file first, then fallback to environment variable"""
    accounts_file = '/app/data/accounts.json'
    try:
        # Try to load from file first
        if os.path.exists(accounts_file):
            with open(accounts_file, 'r') as f:
                accounts = json.load(f)
                app_logger.info(f"Loaded {len(accounts)} accounts from accounts.json")
                return accounts
    except Exception as e:
        app_logger.error(f"Error loading accounts from file: {str(e)}")
    
    # Fall back to environment variable
    accounts_json = os.environ.get('ACCOUNTS', '[]')
    accounts = json.loads(accounts_json)
    app_logger.info(f"Loaded {len(accounts)} accounts from environment variables")
    return accounts

def save_accounts(accounts):
    """Save accounts to persistent storage and update environment"""
    accounts_file = '/app/data/accounts.json'
    os.makedirs(os.path.dirname(accounts_file), exist_ok=True)
    
    # Save to file
    try:
        with open(accounts_file, 'w') as f:
            json.dump(accounts, f)
        app_logger.info(f"Saved {len(accounts)} accounts to accounts.json")
        
        # Also update environment variable for backwards compatibility
        os.environ['ACCOUNTS'] = json.dumps(accounts)
        
        # After saving, restart schedulers to apply changes
        restart_schedulers()
        return True
    except Exception as e:
        app_logger.error(f"Error saving accounts: {str(e)}")
        return False

def restart_schedulers():
    """Restart all scheduler threads with updated accounts"""
    app_logger.info("Restarting scheduler threads...")
    
    # Stop existing threads by setting a flag
    # (This is simulated - in a real implementation, use an event or flag to signal threads)
    
    # Kill existing threads and start new ones
    active_threads = threading.enumerate()
    scheduler_threads = [t for t in active_threads if t.name.startswith("scheduler_")]
    
    app_logger.info(f"Found {len(scheduler_threads)} active scheduler threads")
    
    # We can't easily kill threads in Python, so we'll signal them to stop
    # and start new ones
    
    # Wait a moment for cleanup
    time.sleep(2)
    
    # Start new threads
    run_schedulers()
    
    return True

def check_scheduler_health():
    """Check if scheduler threads are running for all accounts"""
    accounts = load_accounts()
    
    # Count active scheduler threads
    active_threads = threading.enumerate()
    scheduler_threads = [t for t in active_threads if t.name.startswith("scheduler_")]
    
    if len(scheduler_threads) < len(accounts):
        app_logger.warning(f"Only {len(scheduler_threads)}/{len(accounts)} scheduler threads running!")
        return False
    
    app_logger.info(f"Scheduler health check: {len(scheduler_threads)}/{len(accounts)} threads running")
    return True

def get_active_threads():
    """Get list of active threads for monitoring"""
    active_threads = threading.enumerate()
    return [t.name for t in active_threads]

def get_course_status():
    """Get current registration status for all courses"""
    courses = get_all_courses()
    status_dict = {course['id']: {
        'id': course['id'], 
        'name': course['name'],
        'location': course.get('location', ''),
        'timeframe': course.get('timeframe', ''),
        'instructor': course.get('instructor', ''),
        'level': course.get('level', ''),
        'status': 'unknown'
    } for course in courses}
    
# Read credential files to determine registered courses
    accounts = load_accounts()
    for i, account in enumerate(accounts):
        credential_path = f'/app/data/credentials/user{i}.txt'
        if os.path.exists(credential_path):
            with open(credential_path, 'r') as f:
                lines = f.readlines()
                if len(lines) >= 3:
                    courses_line = lines[2].strip()
                    if courses_line:
                        registered_courses = []
                        excluded_courses = []
                        
                        for course in courses_line.split(','):
                            course = course.strip()
                            if course.startswith('!'):
                                excluded_courses.append(course[1:])
                            else:
                                registered_courses.append(course)
                        
                        # Update status
                        for course_id in registered_courses:
                            if course_id in status_dict:
                                status_dict[course_id]['status'] = 'registered'
                        
                        for course_id in excluded_courses:
                            if course_id in status_dict and status_dict[course_id]['status'] != 'registered':
                                status_dict[course_id]['status'] = 'excluded'
    
    # Set remaining courses as available
    for course_id, course in status_dict.items():
        if course['status'] == 'unknown':
            course['status'] = 'available'
    
    return list(status_dict.values())

def get_next_registrations():
    """Get the next scheduled registration times for all courses"""
    current_time = datetime.now()
    upcoming_registrations = []
    
    # Get all courses from mapping
    for course_number, course_info in course_day_time_mapping.items():
        # Calculate next occurrence of this course
        next_course_time = calculate_next_course_time(course_number, current_time)
        if not next_course_time:
            continue
            
        # Calculate time to register (7 minutes before course start)
        registration_time = get_registration_time(next_course_time)
        
        # Only include future registrations
        if registration_time > current_time:
            # Add to list with course details
            upcoming_registrations.append({
                'course_id': course_number,
                'course_name': course_day_mapping.get(course_number, "Unknown course"),
                'registration_time': registration_time.strftime('%Y-%m-%d %H:%M:%S'),
                'time_until': format_time_until(registration_time - current_time)
            })
    
    # Sort by registration time (soonest first)
    upcoming_registrations.sort(key=lambda x: x['registration_time'])
    
    return upcoming_registrations

def format_time_until(time_delta):
    """Format a timedelta into a readable string"""
    total_seconds = int(time_delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m {seconds}s"

def get_log_entries():
    """Get recent log entries"""
    log_entries = []
    log_dir = '/app/data/logs'
    if os.path.exists(log_dir):
        log_files = [f for f in os.listdir(log_dir) if f.startswith('course_scheduler')]
        
        for log_file in log_files:
            account = log_file.replace('course_scheduler.', '').replace('.log', '')
            
            try:
                with open(os.path.join(log_dir, log_file), 'r') as f:
                    # Get the last 100 lines
                    lines = f.readlines()[-100:]
                    
                    for line in lines:
                        entry = parse_log_line(line, account)
                        if entry:
                            log_entries.append(entry)
            except Exception as e:
                app_logger.error(f"Error reading log file {log_file}: {str(e)}")
    
    # Sort by timestamp descending
    log_entries.sort(key=lambda x: x['timestamp'], reverse=True)
    return log_entries

def parse_log_line(line, account):
    """Parse a log line into a structured entry"""
    # Example format: [user0.txt] 2025-05-07 00:09:42,678 - INFO - Skipping: Wednesday 18:00-19:30 (already in configuration)
    import re
    match = re.search(r'\[(.*?)\] (.*?) - (\w+) - (.*)', line)
    if match:
        timestamp_str = match.group(2)
        level = match.group(3)
        message = match.group(4)
        
        # Determine action and status
        action = "check"
        status = "info"
        course = ""
        
        if "Successfully registered for" in message:
            action = "register"
            status = "success"
            course_match = re.search(r'Successfully registered for (.*)', message)
            if course_match:
                course = course_match.group(1)
        elif "Skipping:" in message:
            action = "skip"
            status = "skipped"
            course_match = re.search(r'Skipping: (.*?) \(', message)
            if course_match:
                course = course_match.group(1)
        elif "Error" in message or "ERROR" in level:
            action = "error"
            status = "error"
        elif "Scheduling registration" in message:
            action = "schedule"
            status = "scheduled"
            course_match = re.search(r'Scheduling registration for (.*?) at', message)
            if course_match:
                course = course_match.group(1)
        
        return {
            "timestamp": timestamp_str,
            "account": account,
            "level": level,
            "message": message,
            "action": action,
            "status": status,
            "course": course
        }
    
    return None

def get_all_courses():
    """Get list of all possible courses with detailed information"""
    return [
        {
            "id": "051001", 
            "name": "Wednesday 18:00-19:30",
            "location": "große Unihalle",
            "timeframe": "02.04.-24.09.",
            "instructor": "Timo Klemm",
            "level": "Stufe 1 / Stufe 2"
        },
        {
            "id": "051002", 
            "name": "Friday 16:30-18:00",
            "location": "große Unihalle",
            "timeframe": "04.04.-26.09.",
            "instructor": "Peter Sieck",
            "level": "Stufe 1 / Stufe 2"
        },
        {
            "id": "051003", 
            "name": "Sunday 15:15-16:45",
            "location": "große Unihalle",
            "timeframe": "06.04.-28.09.",
            "instructor": "Timo Klemm",
            "level": "Stufe 1 / Stufe 2"
        },
        {
            "id": "051011", 
            "name": "Tuesday 21:00-22:30",
            "location": "große Unihalle",
            "timeframe": "01.04.-30.09.",
            "instructor": "Timo Bücken, Timo Klemm",
            "level": "Stufe 2 / Stufe 3"
        },
        {
            "id": "051012", 
            "name": "Friday 18:00-19:30",
            "location": "große Unihalle",
            "timeframe": "04.04.-26.09.",
            "instructor": "Peter Sieck",
            "level": "Stufe 2 / Stufe 3"
        },
        {
            "id": "0510011", 
            "name": "Wednesday 18:30-20:30",
            "location": "Baererstraße / Mareststraße - Dreifelhalle",
            "timeframe": "02.04.-24.09.",
            "instructor": "Stefan Zimmer",
            "level": "Stufe 1 / Stufe 2"
        }
    ]

def get_recent_logs(file_path, max_lines=100):
    """Get recent log entries from a log file"""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                # Read all lines but keep only the last max_lines
                lines = f.readlines()[-max_lines:]
                return lines
    except Exception as e:
        app_logger.error(f"Error reading log file {file_path}: {str(e)}")
    return []

# =====================================================================
# Main Execution
# =====================================================================

if __name__ == "__main__":
    # Determine if running as web app or standalone script
    if len(sys.argv) > 1 and sys.argv[1] == "scheduler":
        # Run as standalone scheduler
        if len(sys.argv) < 3:
            print("Usage: python app.py scheduler <credentials_file>")
            sys.exit(1)
        
        credentials_file = sys.argv[2]
        run_registration_scheduler(credentials_file)
    else:
        # Run as web app with schedulers
        
        # Start schedulers in background
        scheduler_thread = threading.Thread(target=run_schedulers)
        scheduler_thread.daemon = True
        scheduler_thread.start()
        
        # Start Flask app
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)
