import os
from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from twilio.rest import Client
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# --- Configuration & Credentials ---
# Google Sheets
# You need a service account JSON file from Google Cloud Console
GCP_SERVICE_ACCOUNT_FILE = os.getenv('GCP_SERVICE_ACCOUNT_FILE', 'openclaw-gmail-agent-491616-bb48f3412eb8.json')
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '1LAc7p_A_9-59_7FCNDgcTeeBDSQMBRAvQZTeRZJ7hww')

# Vapi (For future outbound calls)
VAPI_API_KEY = os.getenv('VAPI_API_KEY', '305e6339-e7ba-46ef-9a9c-981b241c534a')

# Twilio
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', 'AC1e7e453534acd29108c06b5a5021c7c6')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '2ad6dadaaca37121172bff4cfc04dc0')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', '+12602523588')

# Gmail
GMAIL_USER = os.getenv('GMAIL_USER', 'japansmostwanted@gmail.com')
# Need to use an App Password, not your regular Gmail password
GMAIL_APP_PASSWORD = os.getenv('GMAIL_APP_PASSWORD', 'zrdarkpnzlqxtczb')

# --- Initialization ---
gc, sh, issue_log_sheet = None, None, None
twilio_client = None

def init_services():
    global gc, sh, issue_log_sheet, twilio_client
    try:
        gc = gspread.service_account(filename=GCP_SERVICE_ACCOUNT_FILE)
        sh = gc.open_by_key(SPREADSHEET_ID)
        issue_log_sheet = sh.worksheet("IssueLog")
        print("Successfully connected to Google Sheets.")
    except Exception as e:
        print(f"Warning: Could not initialize Google Sheets: {e}")

    try:
        if TWILIO_ACCOUNT_SID != 'YOUR_TWILIO_SID':
            twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            print("Successfully initialized Twilio client.")
    except Exception as e:
        print(f"Warning: Could not initialize Twilio client: {e}")

# Initialize services immediately for Gunicorn compatibility
init_services()

# --- Helper Functions ---
def send_sms(to_phone, body):
    try:
        if not to_phone or not twilio_client:
            return
        message = twilio_client.messages.create(
            body=body,
            from_=TWILIO_PHONE_NUMBER,
            to=to_phone
        )
        print(f"Sent SMS to {to_phone}: {message.sid}")
    except Exception as e:
        print(f"Failed to send SMS: {e}")

def send_email(to_email, subject, body):
    try:
        if not to_email or GMAIL_USER == 'your_email@gmail.com':
            return
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        text = msg.as_string()
        server.sendmail(GMAIL_USER, to_email, text)
        server.quit()
        print(f"Sent Email to {to_email}")
    except Exception as e:
        print(f"Failed to send Email: {e}")

def get_or_create_employee_sheet(employee_name):
    if not sh or not employee_name:
        return None
    
    sheet_title = str(employee_name).strip().title()
    if not sheet_title:
        sheet_title = "Unknown Employee"
        
    try:
        return sh.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        sheet = sh.add_worksheet(title=sheet_title, rows=1000, cols=20)
        headers = [
            "Date", "Employee Name", "Phone", "Job Number", "Clock In", 
            "Lunch Out", "Lunch In", "Job Complete", "Clock Out", 
            "Status", "Issue Reported", "Issue Description", "Call ID", 
            "Transcript", "Supervisor Email", "Last Updated"
        ]
        sheet.append_row(headers)
        return sheet

def find_employee_name_by_phone(phone_number):
    if not sh or not phone_number:
        return None
    try:
        incoming_digits = ''.join(filter(str.isdigit, str(phone_number)))
        if not incoming_digits:
            return None
            
        for sheet in sh.worksheets():
            if sheet.title.lower() == 'records':
                records = sheet.get_all_values()
                for row in records:
                    for i, cell in enumerate(row):
                        stored_digits = ''.join(filter(str.isdigit, str(cell)))
                        if stored_digits and incoming_digits[-10:] == stored_digits[-10:]:
                            name_val = row[0] if row[0] and row[0] != cell else (row[1] if len(row) > 1 else "Unknown")
                            return name_val.strip().title()
                continue
                
            if sheet.title.lower() in ["issuelog", "sheet1", "timelog"]:
                continue
            try:
                val = sheet.acell('C2').value
                if val:
                    stored_digits = ''.join(filter(str.isdigit, str(val)))
                    if stored_digits and incoming_digits[-10:] == stored_digits[-10:]:
                        return sheet.title
            except Exception:
                pass
    except Exception as e:
        print(f"Error finding employee by phone: {e}")
    return None

def find_row_in_sheet(sheet, shift_date, phone, job_number):
    if not sheet:
        return None
    records = sheet.get_all_values()
    # Headers are in row 1
    for idx, row in enumerate(records):
        if idx == 0: continue # Skip header
        if len(row) >= 4:
            row_date = row[0]
            row_phone = row[2]
            row_job = row[3]
            if row_date == shift_date and row_phone == phone and row_job == job_number:
                return idx + 1 # 1-based index for Gspread
    return None

def update_row(sheet, row_num, col_val_dict):
    if not sheet:
        return
    cells = []
    for col, val in col_val_dict.items():
        cells.append(gspread.Cell(row=row_num, col=col, value=str(val)))
    if cells:
        sheet.update_cells(cells)

# --- Webhook Endpoint ---
@app.route('/webhook', methods=['POST'])
def vapi_webhook():
    data = request.json
    print(f"--- INCOMING PAYLOAD ---\n{data}\n------------------------")
    if not data:
        return jsonify({"error": "No JSON payload provided"}), 400

    # Support Vapi's native nested Tool Call payload
    tool_call_id = None
    if 'message' in data:
        msg = data['message']
        msg_type = msg.get('type')
        
        # Intercept call initialization to find returning callers natively
        if msg_type == 'assistant-request':
            caller_phone = msg.get('call', {}).get('customer', {}).get('number', '')
            print(f"Incoming call detected from: {caller_phone}")
            
            e_name = find_employee_name_by_phone(caller_phone)
            
            if e_name:
                print(f"Caller ID Match! Found Employee: {e_name}")
                return jsonify({"assistant": {"variableValues": {"employee_name": e_name, "employee_phone": caller_phone}}}), 200
            else:
                print("New Caller ID. No match found.")
                return jsonify({"assistant": {"variableValues": {"employee_phone": caller_phone}}}), 200

        if msg_type == 'tool-calls':
            tool_calls = msg.get('toolCalls', [])
            if tool_calls:
                tool_call_id = tool_calls[0].get('id')
                func_args = tool_calls[0].get('function', {}).get('arguments', {})
                if isinstance(func_args, str):
                    import json
                    func_args = json.loads(func_args)
                data = func_args

    # Extract required fields from payload
    action_type = data.get('action_type')
    if isinstance(action_type, str):
        action_type = action_type.strip().lower().replace(' ', '_').replace('-', '_')

    employee_name = data.get('employee_name', '')
    employee_phone = data.get('employee_phone', '')
    job_number = data.get('job_number', '')
    issue_description = data.get('issue_description', '')
    call_timestamp = data.get('call_timestamp', datetime.now().strftime('%H:%M:%S'))
    shift_date = data.get('shift_date', '')
    if not shift_date:
        shift_date = datetime.now().strftime('%Y-%m-%d')
    supervisor_email = data.get('supervisor_email', '')
    call_id = data.get('call_id', '')
    raw_transcript = data.get('raw_transcript', '')

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if not action_type:
        return jsonify({"error": "Missing action_type"}), 400

    print(f"Processing webhook: {action_type} for Employee: {employee_name}, Job: {job_number}")

    # Dynamically find or create the employee's personal tab
    employee_sheet = get_or_create_employee_sheet(employee_name)

    # Find existing timesheet record on their personal sheet
    row_num = find_row_in_sheet(employee_sheet, shift_date, employee_phone, job_number)

    if action_type == 'clock_in':
        if employee_sheet:
            new_row = [
                shift_date, employee_name, employee_phone, job_number, 
                call_timestamp, "", "", "", "", "Clocked In", 
                "", "", call_id, raw_transcript, supervisor_email, now_str
            ]
            employee_sheet.append_row(new_row)
        
        send_sms(employee_phone, f"You are clocked in for job {job_number}.")

    elif action_type == 'lunch_out':
        if row_num:
            update_row(employee_sheet, row_num, {6: call_timestamp, 10: "At Lunch", 16: now_str})
        elif employee_sheet:
            employee_sheet.append_row([shift_date, employee_name, employee_phone, job_number, "", call_timestamp, "", "", "", "At Lunch", "", "", call_id, raw_transcript, supervisor_email, now_str])

    elif action_type == 'lunch_in':
        if row_num:
            update_row(employee_sheet, row_num, {7: call_timestamp, 10: "Returned From Lunch", 16: now_str})
        elif employee_sheet:
            employee_sheet.append_row([shift_date, employee_name, employee_phone, job_number, "", "", call_timestamp, "", "", "Returned From Lunch", "", "", call_id, raw_transcript, supervisor_email, now_str])

    elif action_type == 'job_complete':
        if row_num:
            update_row(employee_sheet, row_num, {8: call_timestamp, 10: "Job Complete", 16: now_str})
        elif employee_sheet:
            employee_sheet.append_row([shift_date, employee_name, employee_phone, job_number, "", "", "", call_timestamp, "", "Job Complete", "", "", call_id, raw_transcript, supervisor_email, now_str])

    elif action_type == 'clock_out':
        if row_num:
            update_row(employee_sheet, row_num, {9: call_timestamp, 10: "Clocked Out", 16: now_str})
        elif employee_sheet:
            employee_sheet.append_row([shift_date, employee_name, employee_phone, job_number, "", "", "", "", call_timestamp, "Clocked Out", "", "", call_id, raw_transcript, supervisor_email, now_str])
        send_sms(employee_phone, "You are clocked out for the day.")

    elif action_type == 'work_issue':
        if row_num:
            update_row(employee_sheet, row_num, {
                10: "Issue Reported",
                11: "Yes",
                12: issue_description,
                16: now_str
            })
        
        if issue_log_sheet:
            # 1: Date, 2: Time, 3: Employee Name, 4: Employee Phone, 5: Job Number, 
            # 6: Issue Description, 7: Transcript, 8: Supervisor Email, 9: Call ID, 10: Alert Sent
            new_issue_row = [
                shift_date, call_timestamp, employee_name, employee_phone,
                job_number, issue_description, raw_transcript, supervisor_email,
                call_id, "Yes"
            ]
            issue_log_sheet.append_row(new_issue_row)
        
        # Send Email Alert
        subject = f"JOB ISSUE: {employee_name} - Job #{job_number}"
        html_body = f"""
        <html><body>
        <p><b>Employee:</b> {employee_name}</p>
        <p><b>Phone:</b> {employee_phone}</p>
        <p><b>Job Number:</b> {job_number}</p>
        <p><b>Issue:</b> {issue_description}</p>
        <p><b>Time:</b> {call_timestamp}</p>
        <hr>
        <p><b>Transcript:</b><br>{raw_transcript}</p>
        </body></html>
        """
        send_email(supervisor_email, subject, html_body)

        # Send an SMS to the employee confirming the report
        send_sms(employee_phone, f"Your issue for job {job_number} has been reported.")

    else:
        return jsonify({"error": f"Unknown action_type: {action_type}"}), 400

    # Ensure Vapi receives the strictly required 'results' array back if a tool was called
    if tool_call_id:
        return jsonify({
            "results": [{
                "toolCallId": tool_call_id,
                "result": f"Success: {action_type} recorded."
            }]
        }), 200

    return jsonify({"status": "success", "action": action_type}), 200

if __name__ == '__main__':
    # Run the server locally
    app.run(host='0.0.0.0', port=5050)
