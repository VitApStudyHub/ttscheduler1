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

# Example placeholders
theory_mapping = {
    # fill out your theory mapping
}
lab_mapping = {
    # fill out your lab mapping
}
weekday_map = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6
}

##########################
# 2) Google Auth
##########################
def get_google_calendar_service():
    """
    Check for ?code=... from Google sign-in. If found, fetch_token and store credentials in session.
    If we already have valid creds in session, return a service. Otherwise, return None.
    """
    # If we already have a token in session, try using it
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

    # Otherwise, see if user just returned with ?code=...
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json in the same directory.")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

    # Check if user came back with code=...
    query_params = st.experimental_get_query_params()
    code = query_params.get("code", [None])[0]
    if code:
        try:
            flow.fetch_token(code=code)
            creds = flow.credentials
            st.session_state["google_token"] = pickle.dumps(creds)
            # Immediately remove ?code=... from the URL
            st.experimental_set_query_params()
            st.success("Google authentication successful! You may close the sign-in tab.")
            return build("calendar", "v3", credentials=creds)
        except Exception as e:
            st.error(f"Error fetching token: {e}")
            return None

    # Not authenticated yet
    return None

def open_auth_url_in_new_tab():
    """
    Generate the Google OAuth URL (no 'state' param).
    Attempt to open it in a new tab. Also return the link for manual fallback.
    """
    if not os.path.exists("credentials.json"):
        st.error("Missing credentials.json!")
        return None

    flow = InstalledAppFlow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    # 'include_granted_scopes' removed to avoid "invalid parameter" errors
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline"
    )

    # Attempt auto-open in new tab
    open_script = f"""
    <script>
        window.open("{auth_url}", "_blank");
    </script>
    """
    st.markdown(open_script, unsafe_allow_html=True)

    # Create a custom link that closes the current tab and opens the sign-in in a new tab
    # (May be blocked by some browsers.)
    close_and_open = f"""
    <a href="#"
       onclick="window.open('{auth_url}','_blank'); window.close(); return false;"
       style="color: blue; text-decoration: underline;">
       click here to sign in manually
    </a>
    """
    return close_and_open

##########################
# 3) Timetable Helpers
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
# 4) Calendar Creation
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
    progress_bar = st.progress(0)
    success = True

    for idx, row in df.iterrows():
        course = row.get("Course", "").strip()
        slot_field = row.get("Slot", "").strip()
        venue = row.get("Venue", "").strip()
        faculty = row.get("Faculty Details", "").strip()

        if "EMBEDDED PROJECT" in course.upper():
            continue
        if "NIL-ONL" in venue.upper():
            continue

        summary = f"{course} [{slot_field}]"
        try:
            slot_tokens = [tok.strip().upper() for tok in slot_field.split("+") if tok.strip()]
            is_lab = False
            lab_key = None

            # Check if entire slot_field is in lab_mapping
            if slot_field.upper() in lab_mapping:
                is_lab = True
                lab_key = slot_field.upper()
            else:
                # Otherwise, check each token
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

        progress_val = int(((idx + 1) / total_rows) * 100)
        progress_bar.progress(min(progress_val, 100))

    progress_bar.progress(100)
    return success

##########################
# 5) Multi-Step UI (session-based)
##########################
def main():
    st.title("Get Notifications on Google Calendar!!!")

    # Initialize step
    if "step" not in st.session_state:
        st.session_state["step"] = 1

    # ============= STEP 1: Google Auth + Timetable Input =============
    if st.session_state["step"] == 1:
        st.header("Step 1: Authorize and Upload/Paste Timetable")

        # Attempt to get a service if user has code=... or stored token
        service = get_google_calendar_service()
        if service:
            st.success("You are already authenticated with Google Calendar!")
        else:
            st.warning("Not authenticated. Please sign in first (opens in new tab).")
            if st.button("Sign in with Google"):
                link_html = open_auth_url_in_new_tab()
                if link_html:
                    # Instead of a normal hyperlink, we produce a custom link that closes the current tab
                    # and opens the sign-in in a new tab.
                    st.markdown(
                        f"**If the new tab did not open automatically,** {link_html}",
                        unsafe_allow_html=True
                    )

        st.write("---")
        st.subheader("Upload or Paste Timetable")

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
            text = st.text_area("Paste your timetable text below:", height=300)
            if text:
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

        if st.button("Next -> Step 2"):
            # Check if user is authenticated
            if "google_token" not in st.session_state:
                st.error("Please sign in with Google first!")
                st.stop()
            # Check if user has provided timetable data
            if "df" not in st.session_state:
                st.error("Please upload or paste timetable data first!")
                st.stop()
            st.session_state["step"] = 2
            st.stop()

    # ============= STEP 2: Semester Start Date =============
    elif st.session_state["step"] == 2:
        st.header("Step 2: Select Semester Start Date")
        date_val = st.date_input("Semester Start Date", value=datetime.now().date())
        st.session_state["semester_start"] = date_val

        if st.button("Next -> Step 3"):
            st.session_state["step"] = 3
        st.stop()

    # ============= STEP 3: Timezone =============
    elif st.session_state["step"] == 3:
        st.header("Step 3: Select Timezone")
        tz = st.selectbox("Choose Timezone", ["Asia/Kolkata", "UTC"])
        st.session_state["timezone"] = tz

        if st.button("Next -> Step 4"):
            st.session_state["step"] = 4
        st.stop()

    # ============= STEP 4: Notifications =============
    elif st.session_state["step"] == 4:
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

            submitted = st.form_submit_button("Next -> Step 5")
            if submitted:
                st.session_state["notification_times"] = ntimes
                st.session_state["step"] = 5
        st.stop()

    # ============= STEP 5: Create Calendar Events =============
    elif st.session_state["step"] == 5:
        st.header("Step 5: Create Calendar Events")
        st.write("Now that you have authorized in Step 1, we can create events in your calendar.")

        # Double-check service
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
                            # Remove code param from URL in case it's still there
                            st.experimental_set_query_params()
                            st.success("Calendar events created successfully!")
                            # Remove token so next time user must re-auth
                            if "google_token" in st.session_state:
                                del st.session_state["google_token"]
                            st.info("Token removed. Next time you'll re-auth.")
        else:
            st.warning("You are not authenticated (maybe you reloaded?). Go back to Step 1 to sign in.")
        st.stop()

    else:
        # Invalid step => reset
        st.warning("Invalid step. Resetting to Step 1.")
        st.session_state["step"] = 1
        st.stop()


if __name__ == "__main__":
    main()
