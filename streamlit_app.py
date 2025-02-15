#############################
# streamlit_app.py
#############################

import streamlit as st
import pandas as pd
import os
import pickle
import re
import csv
from io import StringIO
from datetime import datetime, timedelta

# Google OAuth
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# (Optional) If you need ICS functionality
# from ics import Calendar, Event

########################
# 1) Mappings & Constants
########################

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Example theory & lab mappings from your prior code
theory_mapping = {
    "A1": [("TU", "09:00", "09:50"), ("SA", "12:00", "12:50")],
    "A2": [("TU", "15:00", "15:50"), ("SA", "16:00", "16:50")],
    # ... fill out more ...
}
lab_mapping = {
    "L1+L2": [("TU", "08:00", "09:40")],
    "L2+L3": [("TU", "09:00", "10:40")],
    # ... fill out more ...
}

weekday_map = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# Your domain-based redirect URI from Google Cloud console
# EXACTLY match what's in "Authorized redirect URIs"
REDIRECT_URI = "https://vitaptimetablescheduler.streamlit.app"  # Or a sub-path if needed


########################
# 2) Google Auth: Web-based flow
########################

def get_google_calendar_service():
    """
    Attempt a web-based OAuth flow using the domain's base URL as the redirect_uri.
    We'll store credentials in st.session_state["google_token"].
    """
    # 1) If we already have valid creds in session_state, use them
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
        # If still valid
        if creds and creds.valid:
            return build("calendar", "v3", credentials=creds)

    # 2) If no valid creds, do the web flow
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json in the same directory.")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    # Check if user was just redirected back with ?code=...
    query_params = st.experimental_get_query_params()
    if "code" in query_params:
        code = query_params["code"][0]
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            st.session_state["google_token"] = pickle.dumps(creds)
            # Remove ?code=... from URL so user doesn't keep seeing it
            st.experimental_set_query_params()
            st.success("Google authentication successful!")
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            st.error(f"Error fetching token: {e}")
            return None
    else:
        # 3) No code => user hasn't authorized yet. Show link
        auth_url, _ = flow.authorization_url(
            prompt="consent",
            access_type="offline",
            include_granted_scopes="true"
        )
        st.info("**Please authorize access to your Google Calendar**:")
        st.markdown(f"[Sign in with Google]({auth_url})")
        return None


def get_or_create_calendar(service, calendar_name, timezone="Asia/Kolkata"):
    """Create or find a named calendar."""
    if not service:
        return None
    calendars = service.calendarList().list().execute()
    for cal in calendars.get("items", []):
        if cal.get("summary") == calendar_name:
            return cal.get("id")
    body = {"summary": calendar_name, "timeZone": timezone}
    new_cal = service.calendars().insert(body=body).execute()
    return new_cal.get("id")


def get_first_date_on_or_after(start_date, target_weekday):
    """Return the first date on or after start_date that is target_weekday (0=Monday)."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)


########################
# 3) Creating Calendar Events
########################
def create_calendar_events(
    service, df, semester_start_date, calendar_id,
    timezone="Asia/Kolkata", notifications=[]
):
    """
    Creates events from a DataFrame with columns: Course, Slot, Venue, Faculty Details.
    Distinguishes between theory & lab using your 'theory_mapping' & 'lab_mapping'.
    """
    if not service or not calendar_id:
        return False

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

        # Example skipping logic
        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
            slot_tokens = [tok.strip().upper() for tok in slot_field.split("+") if tok.strip()]
            is_lab = False
            lab_key = None

            # 1) If entire slot_field in lab_mapping
            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                # 2) Check sub-tokens
                for tok in slot_tokens:
                    if tok in lab_mapping or tok.startswith("L"):
                        is_lab = True
                        lab_key = tok
                        break

            if is_lab:
                # Lab
                mapping = lab_mapping.get(lab_key)
                if not mapping:
                    st.warning(f"Lab slot '{lab_key}' not found. Skipping row {idx}.")
                    continue
                for day_code, start_str, end_str in mapping:
                    startH, startM = map(int, start_str.split(":"))
                    endH, endM = map(int, end_str.split(":"))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=startH, minute=startM)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=endH, minute=endM)

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
                        st.warning(f"Theory slot '{tok}' not in mapping. Skipping row {idx}.")
                        continue
                    for day_code, start_str, end_str in mapping:
                        startH, startM = map(int, start_str.split(":"))
                        endH, endM = map(int, end_str.split(":"))
                        first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                        dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=startH, minute=startM)
                        dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=endH, minute=endM)

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

        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))

    progress_bar.progress(100)
    return success


########################
# 4) The Multi-Step UI
########################

def main():
    st.title("Get Notifications on Google Calendar!!!")

    # Step state
    if "step" not in st.session_state:
        st.session_state["step"] = 1

    # Step 1: Upload or Paste Timetable
    if st.session_state["step"] == 1:
        st.header("Step 1: Upload Timetable")
        st.write("Choose one of the following to input your timetable:")

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
            st.write("Paste your timetable text below (you must parse it to DataFrame).")
            # For demonstration, we'll skip actual parsing; you can adapt your parse logic
            timetable_text = st.text_area("Timetable Text", height=300)
            if timetable_text:
                # Suppose you have a parse function -> parse_timetable(timetable_text)
                # We'll just do a dummy example:
                # You might produce a DataFrame with columns: ["Course","Slot","Venue","Faculty Details"]
                # For now, let's do a dummy row:
                df = pd.DataFrame([{
                    "Course": "Example Course from text",
                    "Slot": "A1",
                    "Venue": "123-CB",
                    "Faculty Details": "Prof.Example - SCOPE"
                }])
                st.session_state["df"] = df
                st.write("### Parsed Data Preview (Click to edit)")
                edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                st.session_state["df"] = edited_df

        if st.button("Next"):
            if "df" not in st.session_state:
                st.error("Please provide timetable data first!")
            else:
                st.session_state["step"] = 2
            st.stop()

    elif st.session_state["step"] == 2:
        st.header("Step 2: Select Semester Start Date")
        semester_start = st.date_input("Semester Start Date", value=datetime.now().date())
        st.session_state["semester_start"] = semester_start
        if st.button("Next"):
            st.session_state["step"] = 3
        st.stop()

    elif st.session_state["step"] == 3:
        st.header("Step 3: Select Timezone")
        timezone = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state["timezone"] = timezone
        if st.button("Next"):
            st.session_state["step"] = 4
        st.stop()

    elif st.session_state["step"] == 4:
        st.header("Step 4: Set up to 3 Notifications (minutes before event)")
        num_notifications = st.number_input("How many notifications? (0 to 3)",
                                            min_value=0, max_value=3, value=2)
        notification_times = []
        for i in range(num_notifications):
            minutes_before = st.number_input(
                f"Notification {i+1} (minutes before)",
                min_value=1, max_value=1440,
                value=(10 if i == 0 else 5)
            )
            notification_times.append(minutes_before)
        st.session_state["notification_times"] = notification_times

        if st.button("Next"):
            st.session_state["step"] = 5
        st.stop()

    elif st.session_state["step"] == 5:
        st.header("Step 5: Create Calendar Events")
        st.write("We'll now authenticate with Google and create events in your Calendar.")
        if st.button("Create Events Now"):
            # 1) Attempt to get the Google Calendar service
            service = get_google_calendar_service()
            if service:
                # 2) We have a valid service => create or find a calendar
                calendar_id = get_or_create_calendar(
                    service,
                    "Academic Timetable",
                    st.session_state["timezone"]
                )
                if calendar_id:
                    # 3) Create events
                    df = st.session_state["df"]
                    semester_start = st.session_state["semester_start"]
                    notifications = st.session_state["notification_times"]
                    success = create_calendar_events(
                        service, df, semester_start,
                        calendar_id, st.session_state["timezone"],
                        notifications=notifications
                    )
                    if success:
                        st.success("Calendar events created successfully!")
                    else:
                        st.warning("Some events could not be created. Check logs above.")
            else:
                st.warning("Not authenticated yet. Please click the link above to sign in, then re-click 'Create Events Now'.")

        if st.button("Finish/Reset"):
            st.session_state.clear()
        st.stop()


if __name__ == "__main__":
    main()
