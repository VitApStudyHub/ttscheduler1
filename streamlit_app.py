#############################
# streamlit_app.py
#############################
import streamlit as st
import pandas as pd
import os
import pickle
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

#############################
# 1) Constants & Mappings
#############################
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# EXACT domain in your Google console's "Authorized redirect URIs"
REDIRECT_URI = "https://vitaptimetablescheduler.streamlit.app"

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


#############################
# 2) Helper Functions
#############################
def get_first_date_on_or_after(start_date, target_weekday):
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)


def get_or_create_calendar(service, calendar_name, timezone="Asia/Kolkata"):
    if not service:
        return None
    cals = service.calendarList().list().execute()
    for c in cals.get("items", []):
        if c.get("summary") == calendar_name:
            return c.get("id")
    body = {"summary": calendar_name, "timeZone": timezone}
    new_cal = service.calendars().insert(body=body).execute()
    return new_cal.get("id")


def create_calendar_events(service, df, semester_start_date, calendar_id,
                           timezone="Asia/Kolkata", notifications=[]):
    if not service or not calendar_id:
        return False

    overrides = [{"method": "popup", "minutes": m} for m in notifications]
    reminders = {"useDefault": False, "overrides": overrides}

    total_rows = len(df)
    prog_bar = st.progress(0)
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
            slot_tokens = [s.strip().upper() for s in slot_field.split("+") if s.strip()]
            is_lab = False
            lab_key = None

            # Check if entire slot_field is in lab_mapping
            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                # Check sub-tokens
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
                        st.warning(f"Theory slot '{tok}' not found. Skipping row {idx}.")
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

        prog_bar.progress(int(((idx + 1) / total_rows) * 100))

    prog_bar.progress(100)
    return success


#############################
# 3) Google Auth Flow
#############################
def get_google_calendar_service():
    """
    Attempt a web-based OAuth flow with redirect to your domain. 
    We'll detect ?code=... if user is returning from Google sign-in.
    """
    if "google_token" in st.session_state:
        creds = pickle.loads(st.session_state["google_token"])
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

    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json in the same directory.")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    # If user returned with ?code=...
    query_params = st.query_params
    if "code" in query_params:
        code = query_params["code"]
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            st.session_state["google_token"] = pickle.dumps(creds)
            # Clear the code param
            st.set_query_params()
            st.success("Google authentication successful!")
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            st.error(f"Error fetching token: {e}")
            return None

    # If no code => user hasn't authorized yet
    return None


def open_auth_url_in_new_tab():
    """Generate the Google OAuth URL and attempt to open it in a new tab."""
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
        include_granted_scopes="true"
    )

    # Auto-open attempt
    open_script = f"""
    <script>
        window.open("{auth_url}", "_blank");
    </script>
    """
    st.markdown(open_script, unsafe_allow_html=True)

    # Return the URL so user can click if the script fails
    return auth_url


#############################
# 4) Multi-Step UI
#############################
def main():
    st.title("Get Notifications on Google Calendar!!!")

    if "step" not in st.session_state:
        st.session_state["step"] = 1

    # Step 1
    if st.session_state["step"] == 1:
        st.header("Step 1: Upload Timetable")
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
            timetable_text = st.text_area("Paste Timetable Text", height=300)
            if timetable_text:
                # Dummy parse
                df = pd.DataFrame([{
                    "Course": "Example Course from text",
                    "Slot": "A1",
                    "Venue": "123-CB",
                    "Faculty Details": "Prof.Example - SCOPE"
                }])
                st.session_state["df"] = df
                st.write("### Parsed Data Preview")
                edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                st.session_state["df"] = edited_df

        if st.button("Next"):
            if "df" not in st.session_state:
                st.error("Please provide timetable data first!")
            else:
                st.session_state["step"] = 2
            st.stop()

    # Step 2
    elif st.session_state["step"] == 2:
        st.header("Step 2: Select Semester Start Date")
        sem_start = st.date_input("Semester Start", value=datetime.now().date())
        st.session_state["semester_start"] = sem_start
        if st.button("Next"):
            st.session_state["step"] = 3
        st.stop()

    # Step 3
    elif st.session_state["step"] == 3:
        st.header("Step 3: Select Timezone")
        tz = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state["timezone"] = tz
        if st.button("Next"):
            st.session_state["step"] = 4
        st.stop()

    # Step 4
    elif st.session_state["step"] == 4:
        st.header("Step 4: Notifications (minutes before event)")
        num_notifications = st.number_input("How many notifications? (0 to 3)", 0, 3, 2)
        notif_times = []
        for i in range(num_notifications):
            mb = st.number_input(
                f"Notification {i+1} (minutes before)",
                min_value=1, max_value=1440,
                value=(10 if i == 0 else 5)
            )
            notif_times.append(mb)
        st.session_state["notification_times"] = notif_times

        if st.button("Next"):
            st.session_state["step"] = 5
        st.stop()

    # Step 5
    elif st.session_state["step"] == 5:
        st.header("Step 5: Create Calendar Events")

        service = get_google_calendar_service()
        if service:
            st.success("You are authenticated with Google Calendar!")
            if st.button("Create Events Now"):
                cal_id = get_or_create_calendar(service, "Academic Timetable", st.session_state["timezone"])
                if cal_id:
                    df = st.session_state["df"]
                    start_date = st.session_state["semester_start"]
                    notifications = st.session_state["notification_times"]
                    success = create_calendar_events(
                        service, df, start_date, cal_id,
                        st.session_state["timezone"],
                        notifications=notifications
                    )
                    if success:
                        st.success("Calendar events created successfully!")
                        # Remove token so next time we must re-auth
                        if "google_token" in st.session_state:
                            del st.session_state["google_token"]
                            st.info("Token removed. Next time you'll re-auth.")
            st.stop()
        else:
            st.warning("Not authenticated. Please click below to sign in.")
            if st.button("Sign in with Google in new tab"):
                auth_url = open_auth_url_in_new_tab()
                if auth_url:
                    st.write("**If the new tab did not open automatically, please** "
                             f"[click here to sign in manually]({auth_url})")
            st.stop()


if __name__ == "__main__":
    main()
