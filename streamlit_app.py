import streamlit as st
import re
import datetime
import pandas as pd
import os
import pickle
import csv
from io import StringIO
from datetime import datetime, timedelta

# If you need ICS or additional logic
from ics import Calendar, Event
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

#########################
# 1) Constants & Mappings
#########################
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "https://vitaptimetablescheduler.streamlit.app"  # Must match your Google console

# Example placeholders from your prior code
theory_mapping = {
    "A1": [("TU", "09:00", "09:50"), ("SA", "12:00", "12:50")],
    "A2": [("TU", "15:00", "15:50"), ("SA", "16:00", "16:50")],
    # ...
}
lab_mapping = {
    "L1+L2": [("TU", "08:00", "09:40")],
    "L2+L3": [("TU", "09:00", "10:40")],
    # ...
}
weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# ---------------
# Step: HTML tweak to disable Enter key
# ---------------
st.markdown(
    """
    <script>
    document.addEventListener('keydown', function(event) {
      if (event.key === "Enter") {
        event.preventDefault();
      }
    });
    </script>
    """,
    unsafe_allow_html=True
)

#########################
# 2) Google OAuth Helpers
#########################
def get_query_params():
    """Safely get query params, supporting older/newer Streamlit versions."""
    try:
        return st.query_params
    except AttributeError:
        return st.experimental_get_query_params()

def set_query_params(**params):
    """Safely set query params, supporting older/newer Streamlit versions."""
    try:
        st.set_query_params(**params)
    except AttributeError:
        st.experimental_set_query_params(**params)

def get_google_calendar_service():
    """
    A web-based OAuth flow:
    1) If we already have valid credentials in st.session_state, use them.
    2) Else, if ?code=... is in the URL, we fetch_token.
    3) Otherwise, we return None (meaning user is not signed in yet).
    """
    # 1) Check session_state for a stored token
    if "google_token" in st.session_state:
        creds = pickle.loads(st.session_state["google_token"])
        # If they're expired but refreshable, try that
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                st.session_state["google_token"] = pickle.dumps(creds)
            except Exception as e:
                st.error(f"Could not refresh token: {e}")
                del st.session_state["google_token"]
                creds = None
        if creds and creds.valid:
            return build("calendar", "v3", credentials=creds)

    # 2) If we have no local creds, we check if there's a ?code= param in the URL
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json! Please add it to the same directory.")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    query_params = get_query_params()
    if "code" in query_params:
        code = query_params["code"]
        if code:
            try:
                # Build the full "authorization_response" with code param
                full_auth_url = f"{REDIRECT_URI}?code={code}"
                flow.fetch_token(authorization_response=full_auth_url)
                creds = flow.credentials
                st.session_state["google_token"] = pickle.dumps(creds)
                # Clear the code param so it doesn't get reused
                set_query_params()
                st.success("Google authentication successful! You may close the sign-in tab.")
                return build("calendar", "v3", credentials=creds)
            except Exception as e:
                st.error(f"Error fetching token: {e}")
                return None
        else:
            st.warning("Empty 'code' param. Possibly invalid or expired.")
            return None

    # 3) No code => user hasn't authorized
    return None

def open_auth_url_in_new_tab():
    """Generate Google OAuth URL, open in new tab, also return URL for manual click."""
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json!")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes=True
    )
    # Attempt to open in new tab
    open_script = f"""
    <script>
        window.open("{auth_url}", "_blank");
    </script>
    """
    st.markdown(open_script, unsafe_allow_html=True)
    return auth_url

#########################
# 3) Timetable Helpers
#########################
def get_first_date_on_or_after(start_date, target_weekday):
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

def extract_course_details(text):
    """
    Example pattern for extracting courses from raw text.
    Adjust or replace with your actual parse logic as needed.
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
            if course_type.lower() in ["embedded theory", "embedded lab", "theory only"]:
                full_course = course_name
            else:
                full_course = f"{course_name} ({course_type})"
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

#########################
# 4) Creating Calendar Events
#########################
def create_calendar_events(service, df, semester_start_date, calendar_id,
                           timezone="Asia/Kolkata", notifications=[]):
    """
    Creates events from a DataFrame with columns:
    ["Course", "Slot", "Venue", "Faculty Details"].
    Distinguishes between theory & lab using 'theory_mapping' & 'lab_mapping'.
    """
    if not service or not calendar_id:
        return False

    # Build custom reminders
    overrides = [{"method": "popup", "minutes": m} for m in notifications]
    reminders = {"useDefault": False, "overrides": overrides}

    total_rows = len(df)
    progress_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row["Course"].strip()
        slot_field = row["Slot"].strip()
        venue = row["Venue"].strip()
        faculty = row["Faculty Details"].strip()

        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
            slot_tokens = [tok.strip().upper() for tok in slot_field.split("+") if tok.strip()]
            is_lab = False
            lab_key = None

            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                for tok in slot_tokens:
                    if tok in lab_mapping or tok.startswith("L"):
                        is_lab = True
                        lab_key = tok
                        break

            if is_lab:
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found in mapping. Skipping row {idx}.")
                    continue
                for day_code, start_str, end_str in mapping:
                    sh, sm = map(int, start_str.split(":"))
                    eh, em = map(int, end_str.split(":"))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=sh, minute=sm)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=eh, minute=em)
                    event_body = {
                        "summary": summary,
                        "location": venue,
                        "description": faculty,
                        "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                        "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                        "reminders": reminders,
                    }
                    service.events().insert(calendarId=calendar_id, body=event_body).execute()
            else:
                # Theory
                for tok in slot_tokens:
                    mapping = theory_mapping.get(tok)
                    if not mapping:
                        st.warning(f"Theory slot '{tok}' not found in mapping. Skipping row {idx}.")
                        continue
                    for day_code, start_str, end_str in mapping:
                        sh, sm = map(int, start_str.split(":"))
                        eh, em = map(int, end_str.split(":"))
                        first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                        dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=sh, minute=sm)
                        dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=eh, minute=em)
                        event_body = {
                            "summary": summary,
                            "location": venue,
                            "description": faculty,
                            "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                            "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                            "reminders": reminders,
                        }
                        service.events().insert(calendarId=calendar_id, body=event_body).execute()

        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        # Update progress
        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))

    progress_bar.progress(100)
    return success

#########################
# 5) The Multi-Step App
#########################
def main():
    # Keep track of steps
    if "step" not in st.session_state:
        st.session_state["step"] = 1

    st.title("Get Notifications on Google Calendar!!!")

    # Step 1: Upload or Paste Timetable
    if st.session_state["step"] == 1:
        st.header("Step 1: Upload Timetable")
        st.write("Choose one of the following options to input your timetable:")
        input_method = st.radio("Input Method", ["Upload CSV", "Paste Timetable Text"])

        if input_method == "Upload CSV":
            csv_file = st.file_uploader("Upload CSV", type=["csv"])
            if csv_file:
                df = pd.read_csv(csv_file, skipinitialspace=True)
                df.columns = [c.strip() for c in df.columns]
                st.session_state["df"] = df
                st.write("### CSV Preview")
                st.dataframe(df)
        else:
            timetable_text = st.text_area("Paste your timetable text below:", height=300)
            if timetable_text:
                try:
                    courses = extract_course_details(timetable_text)
                    if courses:
                        df = pd.DataFrame(courses)
                        st.session_state["df"] = df
                        st.write("### Parsed Data Preview (Click cells to edit)")
                        edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                        st.session_state["df"] = edited_df
                    else:
                        st.warning("No courses extracted. Check your input format.")
                except Exception as e:
                    st.error(f"Error parsing timetable text: {str(e)}")

        if st.button("Next"):
            if "df" not in st.session_state:
                st.error("Please provide timetable data first!")
            else:
                st.session_state["step"] = 2
            st.stop()

    # Step 2: Semester Start
    elif st.session_state["step"] == 2:
        st.header("Step 2: Select Semester Start Date")
        sem_start = st.date_input("Semester Start Date", value=datetime.now().date())
        st.session_state["semester_start"] = sem_start
        if st.button("Next"):
            st.session_state["step"] = 3
        st.stop()

    # Step 3: Timezone
    elif st.session_state["step"] == 3:
        st.header("Step 3: Select Timezone")
        timezone = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state["timezone"] = timezone
        if st.button("Next"):
            st.session_state["step"] = 4
        st.stop()

    # Step 4: Notifications
    elif st.session_state["step"] == 4:
        st.header("Step 4: Set up to 3 Notifications (minutes before event)")
        with st.form("notification_form"):
            notif_times = []
            for i in range(3):
                mb = st.number_input(
                    f"Notification {i+1} (minutes before, enter 0 to disable)",
                    min_value=0, max_value=1440,
                    value=(10 if i == 0 else 5),
                    key=f"notif_{i}"
                )
                if mb > 0:
                    notif_times.append(mb)

            submit_btn = st.form_submit_button("Next")
            if submit_btn:
                st.session_state["notification_times"] = notif_times
                st.session_state["step"] = 5
            st.stop()

    # Step 5: Create Events
    elif st.session_state["step"] == 5:
        st.header("Step 5: Create Calendar Events")
        st.write("We'll now authenticate with Google and create events in your calendar.")

        # Try to get a service if user is returning with code
        from_google_service = get_google_calendar_service()
        if from_google_service:
            st.success("You are authenticated with Google Calendar!")
            if st.button("Create Events Now"):
                # Now create or find the calendar
                if "df" not in st.session_state:
                    st.error("No timetable data found. Please go back to Step 1.")
                else:
                    calendar_id = get_or_create_calendar(
                        from_google_service,
                        "Academic Timetable",
                        st.session_state["timezone"]
                    )
                    if calendar_id:
                        df = st.session_state["df"]
                        semester_start = st.session_state["semester_start"]
                        notifications = st.session_state["notification_times"]
                        success = create_calendar_events(
                            from_google_service,
                            df,
                            semester_start,
                            calendar_id,
                            st.session_state["timezone"],
                            notifications=notifications
                        )
                        if success:
                            st.success("Calendar events created successfully!")
                            # Optionally remove token so next time user must re-auth
                            if "google_token" in st.session_state:
                                del st.session_state["google_token"]
                                st.info("Token removed. Next time you'll re-auth.")
        else:
            # Not authenticated => show sign-in button
            st.warning("Not authenticated. Please click below to sign in.")
            if st.button("Sign in with Google in new tab"):
                auth_url = open_auth_url_in_new_tab()
                if auth_url:
                    st.write(
                        "**If the new tab did not open automatically,** "
                        f"[click here to sign in manually]({auth_url})"
                    )

        if st.button("Finish/Reset"):
            st.session_state.clear()
        st.stop()

if __name__ == "__main__":
    main()
