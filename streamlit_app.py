import streamlit as st
import re
import datetime
import pandas as pd
import os
import pickle
import csv
from io import StringIO
from datetime import datetime, timedelta
from ics import Calendar, Event
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------- Disable Enter Key ----------------
st.markdown(
    """
    <script>
    document.addEventListener('keydown', function(event) {
      // Prevent default action if Enter is pressed
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    </script>
    """,
    unsafe_allow_html=True
)

# ---------------- Google Calendar Setup ----------------
SCOPES = ['https://www.googleapis.com/auth/calendar']

def authenticate_google_calendar():
    creds = None
    if 'google_token' in st.session_state:
        creds = pickle.loads(st.session_state['google_token'])
    if not creds or not creds.valid:
        from google.auth.transport.requests import Request
        if creds and creds.refresh_token and creds.expired:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                st.error("No credentials.json found! Please place it in the same directory.")
                return None
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            st.info("A new browser window will open for authentication.")
            # Custom success message that automatically closes the browser window
            success_message = """
            Authentication successful. You may close this window.
            """
            try:
                # The auth link will expire after 30 seconds if no action is taken.
                creds = flow.run_local_server(port=8080, success_message=success_message, timeout=30)
            except Exception as e:
                st.error("Authentication failed or timed out. Please try again.")
                return None
        st.session_state['google_token'] = pickle.dumps(creds)
    return build('calendar', 'v3', credentials=creds)

def get_or_create_calendar(service, calendar_name, timezone):
    if not service:
        return None
    result = service.calendarList().list().execute()
    for cal in result.get('items', []):
        if cal.get('summary') == calendar_name:
            return cal.get('id')
    body = {
        'summary': calendar_name,
        'timeZone': timezone,
    }
    created = service.calendars().insert(body=body).execute()
    return created.get('id')

def get_first_date_on_or_after(start_date, target_weekday):
    """Return the first date on or after start_date that falls on target_weekday (0=Monday)."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

# ---------------- Course Extraction ----------------
def extract_course_details(text):
    """
    Extract course details from timetable text.
    This pattern accepts course names with Roman numerals (e.g. IV) and multi-word names.
    Expected format example:
    
        10
        General (Semester)
        ENG2009 - Business Communication and Value Science - IV
        ( Theory Only )
        2 0 0 0 2.0
        - Regular
        AP2024254000012
        F1 -
        213-CB
        Prof.Karishma Bisht -
        VISH
        11-Dec-2024 16:00
        12-Dec-2024
        - Manual
        Subject to Offering
    """
    course_pattern = re.compile(
        r"(\w+\d+ - [\w\s-]+(?:\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X))?)\s*\(([\w\s]+)\)\s*"
        r"[\d\s.]+\s*-\s*Regular\s*([\w\d]+)\s*([\w\d\+\-]+)\s*-\s*([\w\d-]+)\s*([\w\s.]+)\s*-\s*([\w]+)",
        re.IGNORECASE
    )
    courses = []
    for match in course_pattern.finditer(text):
        try:
            course_name = match.group(1).strip()
            course_type = match.group(2).strip()
            # If course_type is one of these, remove it from the final title.
            if course_type.lower() in ["embedded theory", "embedded lab", "theory only"]:
                full_course = course_name
            else:
                full_course = f"{course_name} ({course_type})"
            # group(3) is an extra code we ignore here
            slot = match.group(4).strip()
            venue = match.group(5).strip()
            faculty = f"{match.group(6).strip()} - {match.group(7).strip()}"
            courses.append({
                "Course": full_course,
                "Slot": slot,
                "Venue": venue,
                "Faculty Details": faculty,
            })
        except Exception as e:
            st.error(f"Error processing course: {str(e)}")
            continue
    return courses

# ---------------- ICS & Event Generation ----------------
def get_event_datetime(slot_times, start_date):
    """Generate datetime objects for events based on slot times."""
    event_times = []
    for day, start_time, end_time in slot_times:
        event_date = start_date + timedelta(days=weekday_map[day])
        start_dt = datetime.combine(event_date, datetime.strptime(start_time, "%H:%M").time())
        end_dt = datetime.combine(event_date, datetime.strptime(end_time, "%H:%M").time())
        event_times.append((start_dt, end_dt))
    return event_times

def generate_calendar(events):
    """Generate an ICS calendar file from event details."""
    cal = Calendar()
    for event in events:
        e = Event()
        e.name = event["title"]
        e.begin = event["start_time"]
        e.end = event["end_time"]
        e.location = event["venue"]
        e.description = event["faculty"]
        cal.events.add(e)
    return cal

def create_calendar_events(service, df, semester_start_date, calendar_id,
                           timezone="Asia/Kolkata", notifications=[]):
    """
    Creates events from a DataFrame with columns: Course, Slot, Venue, Faculty Details.
    Distinguishes between theory and lab slots based on the slot tokens.
    """
    if not service or not calendar_id:
        return False

    overrides = [{'method': 'popup', 'minutes': m} for m in notifications]
    reminders = {
        'useDefault': False,
        'overrides': overrides
    }

    total_rows = len(df)
    progress_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row['Course'].strip()
        slot_field = row['Slot'].strip()
        venue = row['Venue'].strip()
        faculty = row['Faculty Details'].strip()

        # Skip events that indicate embedded projects or "NIL-ONL" venue.
        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
            # Determine whether the slot indicates a lab.
            slot_tokens = [tok.strip().upper() for tok in slot_field.split('+')]
            is_lab = False
            lab_key = None

            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                for tok in slot_tokens:
                    if tok in lab_mapping or tok.startswith('L'):
                        is_lab = True
                        lab_key = tok
                        break

            if is_lab:
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found in mapping. Skipping.")
                    continue
                for day_code, start_str, end_str in mapping:
                    start_hour, start_minute = map(int, start_str.split(':'))
                    end_hour, end_minute = map(int, end_str.split(':'))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                    event = {
                        'summary': summary,
                        'location': venue,
                        'description': faculty,
                        'start': {'dateTime': dtstart.isoformat(), 'timeZone': timezone},
                        'end': {'dateTime': dtend.isoformat(), 'timeZone': timezone},
                        'recurrence': [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                        'reminders': reminders,
                    }
                    service.events().insert(calendarId=calendar_id, body=event).execute()
            else:
                # Process theory events by checking each token.
                for tok in slot_tokens:
                    mapping = theory_mapping.get(tok)
                    if not mapping:
                        st.warning(f"Theory slot '{tok}' not found in mapping. Skipping.")
                        continue
                    for day_code, start_str, end_str in mapping:
                        start_hour, start_minute = map(int, start_str.split(':'))
                        end_hour, end_minute = map(int, end_str.split(':'))
                        first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                        dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=start_hour, minute=start_minute)
                        dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=end_hour, minute=end_minute)
                        event = {
                            'summary': summary,
                            'location': venue,
                            'description': faculty,
                            'start': {'dateTime': dtstart.isoformat(), 'timeZone': timezone},
                            'end': {'dateTime': dtend.isoformat(), 'timeZone': timezone},
                            'recurrence': [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                            'reminders': reminders,
                        }
                        service.events().insert(calendarId=calendar_id, body=event).execute()
        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))
    progress_bar.progress(100)
    return success

# ---------------- Mappings ----------------
theory_mapping = {
    "A1": [("TU", "09:00", "09:50"), ("SA", "12:00", "12:50")],
    "A2": [("TU", "15:00", "15:50"), ("SA", "16:00", "16:50")],
    "B1": [("TU", "10:00", "10:50"), ("WE", "12:00", "12:50")],
    "B2": [("WE", "17:00", "17:50"), ("TH", "14:00", "14:50")],
    "C1": [("TU", "11:00", "11:50"), ("SA", "10:00", "10:50")],
    "C2": [("TU", "17:00", "17:50"), ("FR", "14:00", "14:50")],
    "D1": [("TU", "12:00", "12:50"), ("WE", "09:00", "09:50")],
    "D2": [("WE", "14:00", "14:50"), ("SA", "14:00", "14:50")],
    "E1": [("WE", "11:00", "11:50"), ("SA", "09:00", "09:50")],
    "E2": [("TU", "14:00", "14:50"), ("WE", "15:00", "15:50")],
    "F1": [("WE", "10:00", "10:50"), ("FR", "11:00", "11:50")],
    "F2": [("WE", "16:00", "16:50"), ("TH", "15:00", "15:50")],
    "G1": [("FR", "10:00", "10:50"), ("SA", "11:00", "11:50")],
    "G2": [("TU", "16:00", "16:50"), ("FR", "16:00", "16:50")],
    "TA1": [("TH", "11:00", "11:50")],
    "TA2": [("TH", "17:00", "17:50")],
    "TB1": [("FR", "09:00", "09:50")],
    "TB2": [("FR", "15:00", "15:50")],
    "TC1": [("TH", "09:00", "09:50")],
    "TC2": [("SA", "15:00", "15:50")],
    "TD1": [("TH", "10:00", "10:50")],
    "TD2": [("TH", "16:00", "16:50")],
    "TE1": [("FR", "12:00", "12:50")],
    "TE2": [("FR", "17:00", "17:50")],
    "TF1": [("TH", "08:00", "08:50")],
    "TF2": [("TH", "18:00", "18:50")],
    "TG1": [("WE", "08:00", "08:50")],
    "TG2": [("SA", "18:00", "18:50")],
    "TAA1": [("FR", "10:00", "10:50")],
    "TBB1": [("SA", "11:00", "11:50")],
    "TCC1": [("FR", "08:00", "08:50")],
    "TDD1": [("SA", "08:00", "08:50")],
    "TEE1": [("TU", "08:00", "08:50")],
    "TFF1": [("TH", "12:00", "12:50")],
    "TAA2": [("FR", "16:00", "16:50")],
    "TBB2": [("TU", "16:00", "16:50")],
    "TCC2": [("WE", "18:00", "18:50")],
    "TDD2": [("TU", "18:00", "18:50")],
    "TEE2": [("TH", "18:00", "18:50")],
    "SC2": [("WE", "11:00", "11:50")],
    "CLUBS": [("TH", "12:00", "12:50"), ("SA", "17:00", "17:50"), ("SA", "18:00", "18:50")],
    "ECS": [("TH", "12:00", "12:50"), ("SA", "17:00", "17:50"), ("SA", "18:00", "18:50")],
    "SD2": [("FR", "12:00", "12:50")],
    "SE2": [("SA", "09:00", "09:50")],
    "SE1": [("TU", "14:00", "14:50")],
    "SC1": [("WE", "15:00", "15:50")],
    "SD1": [("FR", "17:00", "17:50")],
    "SF1": [("SA", "17:00", "17:50")]
}

lab_mapping = {
    "L1+L2": [("TU", "08:00", "09:40")],
    "L2+L3": [("TU", "09:00", "10:40")],
    "L3+L4": [("TU", "10:00", "11:40")],
    "L4+L5": [("TU", "11:00", "12:40")],
    "L5+L6": [("TU", "12:00", "13:30")],
    "L7+L8": [("WE", "08:00", "09:40")],
    "L8+L9": [("WE", "09:00", "10:40")],
    "L9+L10": [("WE", "10:00", "11:40")],
    "L10+L11": [("WE", "11:00", "12:40")],
    "L11+L12": [("WE", "12:00", "13:30")],
    "L13+L14": [("TH", "08:00", "09:40")],
    "L14+L15": [("TH", "09:00", "10:40")],
    "L15+L16": [("TH", "10:00", "11:40")],
    "L16+L17": [("TH", "11:00", "12:40")],
    "L17+L18": [("TH", "12:00", "13:30")],
    "L19+L20": [("FR", "08:00", "09:40")],
    "L20+L21": [("FR", "09:00", "10:40")],
    "L21+L22": [("FR", "10:00", "11:40")],
    "L22+L23": [("FR", "11:00", "12:40")],
    "L23+L24": [("FR", "12:00", "13:30")],
    "L25+L26": [("SA", "08:00", "09:40")],
    "L26+L27": [("SA", "09:00", "10:40")],
    "L27+L28": [("SA", "10:00", "11:40")],
    "L28+L29": [("SA", "11:00", "12:40")],
    "L29+L30": [("SA", "12:00", "13:30")],
    "L31+L32": [("TU", "14:00", "15:40")],
    "L32+L33": [("TU", "15:00", "16:40")],
    "L33+L34": [("TU", "16:00", "17:40")],
    "L34+L35": [("TU", "17:00", "18:40")],
    "L35+L36": [("TU", "18:00", "19:30")],
    "L37+L38": [("WE", "14:00", "15:40")],
    "L38+L39": [("WE", "15:00", "16:40")],
    "L39+L40": [("WE", "16:00", "17:40")],
    "L40+L41": [("WE", "17:00", "18:40")],
    "L41+L42": [("WE", "18:00", "19:30")],
    "L43+L44": [("TH", "14:00", "15:40")],
    "L44+L45": [("TH", "15:00", "16:40")],
    "L45+L46": [("TH", "16:00", "17:40")],
    "L46+L47": [("TH", "17:00", "18:40")],
    "L47+L48": [("TH", "18:00", "19:30")],
    "L49+L50": [("FR", "14:00", "15:40")],
    "L50+L51": [("FR", "15:00", "16:40")],
    "L51+L52": [("FR", "16:00", "17:40")],
    "L52+L53": [("FR", "17:00", "18:40")],
    "L53+L54": [("FR", "18:00", "19:30")],
    "L55+L56": [("SA", "14:00", "15:40")],
    "L56+L57": [("SA", "15:00", "16:40")],
    "L57+L58": [("SA", "16:00", "17:40")],
    "L58+L59": [("SA", "17:00", "18:40")],
    "L59+L60": [("SA", "18:00", "19:30")]
}

weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# ---------------- Main Multi-Step App ----------------
def main():
    if 'step' not in st.session_state:
        st.session_state['step'] = 1

    st.title("Get Notifications on Google Calendar!!!")

    # Step 1: Upload or Paste Timetable
    if st.session_state['step'] == 1:
        st.header("Step 1: Upload Timetable")
        st.write("Choose one of the following options to input your timetable:")
        input_method = st.radio("Input Method", ["Upload CSV", "Paste Timetable Text"])

        if input_method == "Upload CSV":
            st.write("Upload your timetable CSV with columns: Course, Slot, Venue, Faculty Details.")
            csv_file = st.file_uploader("Upload CSV", type=['csv'])
            if csv_file:
                df = pd.read_csv(csv_file, skipinitialspace=True)
                df.columns = [c.strip() for c in df.columns]
                st.session_state['df'] = df
                st.write("### CSV Preview")
                st.dataframe(df)
        else:
            st.write("Paste your timetable text below:")
            timetable_text = st.text_area("Timetable Text", height=300)
            if timetable_text:
                try:
                    courses = extract_course_details(timetable_text)
                    if courses:
                        df = pd.DataFrame(courses)
                        st.session_state['df'] = df
                        st.write("### Parsed Data Preview (Click cells to edit)")
                        edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                        st.session_state['df'] = edited_df
                    else:
                        st.warning("No courses extracted. Check the input format.")
                except Exception as e:
                    st.error(f"Error parsing timetable text: {str(e)}")
        if st.button("Next"):
            if 'df' not in st.session_state:
                st.error("Please provide timetable data first!")
            else:
                st.session_state['step'] = 2
            st.stop()

    # Step 2: Select Semester Start Date
    elif st.session_state['step'] == 2:
        st.header("Step 2: Select Semester Start Date")
        semester_start = st.date_input("Semester Start Date", min_value=datetime.now().date())
        st.session_state['semester_start'] = semester_start
        if st.button("Next"):
            st.session_state['step'] = 3
        st.stop()

    # Step 3: Select Timezone
    elif st.session_state['step'] == 3:
        st.header("Step 3: Select Timezone")
        timezone = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state['timezone'] = timezone
        if st.button("Next"):
            st.session_state['step'] = 4
        st.stop()

    # Step 4: Set Notifications
    elif st.session_state['step'] == 4:
        st.header("Step 4: Set up to 3 Notifications (minutes before event)")
        with st.form("notification_form"):
            notification_times = []
            # Allow a value of 0 to indicate that this notification should not be used.
            for i in range(3):
                minutes_before = st.number_input(
                    f"Notification {i+1} (minutes before, enter 0 to disable)",
                    min_value=0, max_value=1440,
                    value=(10 if i == 0 else 5),
                    key=f"notif_{i}"
                )
                if minutes_before > 0:
                    notification_times.append(minutes_before)
            submit_notifications = st.form_submit_button("Next")
            if submit_notifications:
                st.session_state['notification_times'] = notification_times
                st.session_state['step'] = 5
                st.stop()

    # Step 5: Create Calendar Events
    elif st.session_state['step'] == 5:
        st.header("Step 5: Create Calendar Events")
        st.write("Click the button below to authenticate with Google and create events in your calendar.")
        if st.button("Create Events Now"):
            if 'df' not in st.session_state:
                st.error("No timetable data found. Please go back.")
            else:
                with st.spinner("Creating calendar events..."):
                    df = st.session_state['df']
                    required_cols = {"Course", "Slot", "Venue", "Faculty Details"}
                    if not required_cols.issubset(set(df.columns)):
                        st.error(f"CSV must have columns: {required_cols}")
                    else:
                        service = authenticate_google_calendar()
                        if service:
                            calendar_id = get_or_create_calendar(
                                service,
                                "Academic Timetable",
                                st.session_state['timezone']
                            )
                            if calendar_id:
                                success = create_calendar_events(
                                    service,
                                    df,
                                    st.session_state['semester_start'],
                                    calendar_id,
                                    st.session_state['timezone'],
                                    notifications=st.session_state['notification_times']
                                )
                                if success:
                                    st.success("✅ Calendar events created successfully!")
                                    st.info("You can now close the sign-in window if it hasn't closed automatically.")
                                else:
                                    st.warning("⚠ Some events could not be created. Check errors above.")
                        else:
                            st.error("Google Calendar authentication failed.")
        if st.button("Finish/Reset"):
            st.session_state.clear()
        st.stop()

if __name__ == "__main__":
    main()
