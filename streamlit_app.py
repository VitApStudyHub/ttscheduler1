import streamlit as st
import pandas as pd
import os
import pickle
import re
from datetime import datetime, timedelta

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

##########################
# 1) Constants & Mappings
##########################
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "https://vitaptimetablescheduler.streamlit.app"

theory_mapping = {
    # ...
}
lab_mapping = {
    # ...
}
weekday_map = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6
}

##########################
# 2) Query Param Helpers
##########################
def get_query_params():
    """Safely get query params (supports older Streamlit versions)."""
    try:
        return st.query_params
    except AttributeError:
        return st.experimental_get_query_params()

def set_query_params(**params):
    """Safely set query params (supports older Streamlit versions)."""
    try:
        st.set_query_params(**params)
    except AttributeError:
        st.experimental_set_query_params(**params)

def get_current_step():
    """Return the current step from ?step=..., default=1 if missing or invalid."""
    query = get_query_params()
    step_str = query.get("step", ["1"])[0]  # e.g. '1'
    try:
        step = int(step_str)
    except ValueError:
        step = 1
    return step

def go_to_step(step_number):
    """
    Helper to navigate to a given step by setting ?step=step_number
    and stopping execution so the user sees the new step immediately.
    """
    set_query_params(step=str(step_number))
    st.stop()

##########################
# 3) Google Auth Flow
##########################
def get_google_calendar_service():
    """
    Web-based OAuth flow:
      - If we have valid creds in st.session_state, use them.
      - Else if ?code=... in the URL, fetch_token and store creds.
      - Otherwise, return None => not signed in.

    Also uses the 'state' param to recover the step user was on.
    """
    # 1) If we already have a stored token
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

    # 2) If no local creds, see if user returned with ?code=... from Google
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json in the same directory.")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    query = get_query_params()
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]  # The step we stored
    if code:
        try:
            # Attempt to exchange the auth code for credentials
            flow.fetch_token(code=code)
            creds = flow.credentials
            st.session_state["google_token"] = pickle.dumps(creds)

            # Restore the step from 'state' if present
            if state and state.isdigit():
                set_query_params(step=state)
            else:
                # fallback to step 5 if no state was found
                set_query_params(step="5")

            st.success("Google authentication successful! You may close the sign-in tab.")
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            st.error(f"Error fetching token: {e}")
            return None

    return None  # Not authenticated yet

def open_auth_url_in_new_tab():
    """
    Generate the Google OAuth URL, passing 'state' as the current step,
    open in a new tab, and return the link for manual fallback.
    """
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json!")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    current_step = get_current_step()
    # Removed include_granted_scopes to avoid "invalid parameter" errors
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        state=str(current_step)  # store the current step in 'state'
    )

    # Attempt auto-open
    open_script = f"""
    <script>
        window.open("{auth_url}", "_blank");
    </script>
    """
    st.markdown(open_script, unsafe_allow_html=True)
    return auth_url

##########################
# 4) Timetable Helpers
##########################
def extract_course_details(text):
    """
    Example parser for your text-based timetable.
    Adjust or replace with your actual parse logic.
    """
    pattern = re.compile(
        r"(\w+\d+ - [\w\s-]+(?:\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X))?)\s*\(([\w\s]+)\)\s*"
        r"[\d\s.]+\s*-\s*Regular\s*([\w\d]+)\s*([\w\d\+\-]+)\s*-\s*([\w\d-]+)\s*([\w\s.]+)\s*-\s*([\w]+)",
        re.IGNORECASE
    )
    courses = []
    for match in pattern.finditer(text):
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

def get_first_date_on_or_after(start_date, target_weekday):
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)

##########################
# 5) Calendar Creation
##########################
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
    prog = st.progress(0)
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
                    st.warning(f"Lab slot '{lab_key}' not found. Skipping row {idx}.")
                    continue
                for day_code, start_str, end_str in mapping:
                    sh, sm = map(int, start_str.split(":"))
                    eh, em = map(int, end_str.split(":"))
                    first_date = get_first_date_on_or_after(semester_start_date, weekday_map[day_code])
                    dtstart = datetime.combine(first_date, datetime.min.time()).replace(hour=sh, minute=sm)
                    dtend = datetime.combine(first_date, datetime.min.time()).replace(hour=eh, minute=em)
                    event = {
                        "summary": summary,
                        "location": venue,
                        "description": faculty,
                        "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                        "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                        "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                        "reminders": reminders,
                    }
                    service.events().insert(calendarId=calendar_id, body=event).execute()
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
                        event = {
                            "summary": summary,
                            "location": venue,
                            "description": faculty,
                            "start": {"dateTime": dtstart.isoformat(), "timeZone": timezone},
                            "end": {"dateTime": dtend.isoformat(), "timeZone": timezone},
                            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={day_code}"],
                            "reminders": reminders,
                        }
                        service.events().insert(calendarId=calendar_id, body=event).execute()
        except Exception as e:
            st.error(f"Error creating event for {course}: {str(e)}")
            success = False

        prog.progress(int(((idx + 1) / total_rows) * 100))

    prog.progress(100)
    return success

##########################
# 6) The Multi-Step UI
##########################
def main():
    st.title("Get Notifications on Google Calendar!!!")

    step = get_current_step()

    # Step 1
    if step == 1:
        st.header("Step 1: Upload Timetable")
        method = st.radio("Input Method", ["Upload CSV", "Paste Timetable Text"])

        if method == "Upload CSV":
            csv_file = st.file_uploader("Upload CSV", type=["csv"])
            if csv_file:
                df = pd.read_csv(csv_file, skipinitialspace=True)
                df.columns = [c.strip() for c in df.columns]
                st.session_state["df"] = df
                st.write("### CSV Preview")
                st.dataframe(df)
        else:
            text = st.text_area("Paste your timetable text below:", height=300)
            if text:
                # Example parse
                try:
                    courses = extract_course_details(text)
                    if courses:
                        df = pd.DataFrame(courses)
                        st.session_state["df"] = df
                        st.write("### Parsed Data Preview (click to edit)")
                        edited_df = st.data_editor(df, num_rows="dynamic", key="editor")
                        st.session_state["df"] = edited_df
                    else:
                        st.warning("No courses extracted. Check your input format.")
                except Exception as e:
                    st.error(f"Error parsing: {e}")

        if st.button("Next"):
            if "df" not in st.session_state:
                st.error("Please provide timetable data first!")
            else:
                go_to_step(2)

    elif step == 2:
        st.header("Step 2: Select Semester Start Date")
        date_val = st.date_input("Semester Start Date", value=datetime.now().date())
        st.session_state["semester_start"] = date_val
        if st.button("Next"):
            go_to_step(3)

    elif step == 3:
        st.header("Step 3: Select Timezone")
        tz = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state["timezone"] = tz
        if st.button("Next"):
            go_to_step(4)

    elif step == 4:
        st.header("Step 4: Notifications (minutes before event)")
        with st.form("notif_form"):
            ntimes = []
            for i in range(3):
                m = st.number_input(
                    f"Notification {i+1} (minutes before, enter 0 to disable)",
                    min_value=0, max_value=1440,
                    value=(10 if i == 0 else 5),
                    key=f"notif_{i}"
                )
                if m > 0:
                    ntimes.append(m)

            submitted = st.form_submit_button("Next")
            if submitted:
                st.session_state["notification_times"] = ntimes
                go_to_step(5)

    elif step == 5:
        st.header("Step 5: Create Calendar Events")
        st.write("We'll now authenticate with Google and create events in your calendar.")

        service = get_google_calendar_service()
        if service:
            st.success("You are authenticated with Google Calendar!")
            if st.button("Create Events Now"):
                if "df" not in st.session_state:
                    st.error("No timetable data found. Please go back to Step 1.")
                else:
                    calendar_id = get_or_create_calendar(
                        service,
                        "Academic Timetable",
                        st.session_state.get("timezone", "Asia/Kolkata")
                    )
                    if calendar_id:
                        df = st.session_state["df"]
                        semester_start = st.session_state.get("semester_start", datetime.now().date())
                        notifs = st.session_state.get("notification_times", [])
                        success = create_calendar_events(
                            service,
                            df,
                            semester_start,
                            calendar_id,
                            st.session_state.get("timezone", "Asia/Kolkata"),
                            notifications=notifs
                        )
                        if success:
                            st.success("Calendar events created successfully!")
                            if "google_token" in st.session_state:
                                del st.session_state["google_token"]
                                st.info("Token removed. Next time you'll re-auth.")
        else:
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
            set_query_params(step="1")

    else:
        st.warning("Invalid step. Redirecting to step 1...")
        go_to_step(1)

if __name__ == "__main__":
    main()
