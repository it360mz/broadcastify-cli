import os
import re
import json
import random
import datetime
import glob
import warnings

import requests
import click

from faster_whisper import WhisperModel
from rich.console import Console
from rich.progress import track
from pathlib import Path
from pydub.utils import which
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

import shutil

import ffmpeg
import tempfile

from pyannote.audio import Pipeline

load_dotenv(".env")
warnings.filterwarnings("ignore", module="whisper")

BRODCASTIFY_CLI_VERSION = "0.1.2"

console = Console()

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(BRODCASTIFY_CLI_VERSION, message='brodcastify-cli version: %(version)s')
def cli():
    pass


@cli.command("download", help="Download archives by date and feed id")
@click.option("--feed-id", "-id", required=True, help="Broadcastify feed id")
@click.option("--date", "-d", required=False, help="Date in format YYYY/MM/DD") 
@click.option("--range", "-r", required=False, help="Date range in format YYYY/MM/DD-YYYY/MM/DD")
@click.option("--past-days", "-p", type=int, required=False, help="Download archives from the past n days")
@click.option("--combine", is_flag=True, help="Combine downloaded MP3 files into a single file")
@click.option("--transcribe", "-t", is_flag=True, help="Transcribe downloaded MP3 files")
@click.option("--gpu", is_flag=True, help="Use GPU for transcription")
@click.option("--jobs", "-j", type=int, default=1, help="Number of concurrent download jobs")
@click.option("--model-size", type=click.Choice(["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "distil-medium.en", "distil-large-v2", "distil-large-v3"]), default="distil-large-v3", help="Whisper model size to use for transcription")
@click.option("--output-dir", "-o", type=click.Path(file_okay=False, dir_okay=True), default="archives", help="Custom output directory for downloaded files")
def download(feed_id, date, range, past_days, combine, transcribe, jobs, gpu, model_size, output_dir):

    user_agent = get_urser_agent()
    login_cookie = get_login_cookie(user_agent)

    if login_cookie is None:
        print("Failed to get login cookie")
        return

    if date:
        console.print(f"Downloading archives for feed id: {feed_id} on {date}")
        download_archive_by_date(feed_id, date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)
        console.print(f"Download complete: {output_dir}/{feed_id}/{date.replace('/', '')}")
    elif range:
        start_date, end_date = range.split("-")
        console.print(f"Downloading archives for feed id: {feed_id} from {start_date} to {end_date}")
        download_archives_by_range(feed_id, start_date, end_date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)
        console.print(f"Download complete: {output_dir}/{feed_id}")
    elif past_days:
        console.print(f"Downloading archives for feed id: {feed_id} from the past {past_days} days")
        download_past_n_days(feed_id, past_days, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)
    else:
        console.print(f"Downloading all archives for feed id: {feed_id}")    
        download_all_archives(feed_id, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)
    
    console.print(f"Download complete: {output_dir}/{feed_id}")


@cli.command("transcribe", help="Transcribe directory of audio files")
@click.option("--directory", "-d", required=True, help="Directory containing audio files")
@click.option("--gpu", is_flag=True, help="Use GPU for transcription")
@click.option("--model-size", type=click.Choice(["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "distil-medium.en", "distil-large-v2", "distil-large-v3"]), default="distil-large-v3", help="Whisper model size to use for transcription")
def transcribe(directory, gpu, model_size):
    transcribe_audio(directory, gpu, model_size)


def download_archives_by_range(feed_id, start_date, end_date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size):

    today = datetime.datetime.now().strftime("%Y/%m/%d")

    if start_date > today or end_date > today:
        console.print("[red]Error:[/red] Invalid date range, start date and end date must be before today")
        exit(1)

    if start_date > end_date:
        console.print("[red]Error:[/red] Invalid date range, start date must be before end date")
        exit(1)


    start_date = datetime.datetime.strptime(start_date, "%Y/%m/%d")
    end_date = datetime.datetime.strptime(end_date, "%Y/%m/%d")

    dates = []

    while start_date <= end_date:
        dates.append(start_date.strftime("%Y/%m/%d"))
        start_date += datetime.timedelta(days=1)

    for date in dates:
        download_archive_by_date(feed_id, date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)


def download_all_archives(feed_id, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size, days=365):
    dates = []
    current_date = datetime.datetime.now()
    start_date = current_date - datetime.timedelta(days=days)

    while current_date >= start_date:
        dates.append(current_date.strftime("%Y/%m/%d"))
        current_date -= datetime.timedelta(days=1)

    for date in dates:
        download_archive_by_date(feed_id, date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size)


def download_archive_by_date(feed_id, date, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size):

    base_download_url = "https://www.broadcastify.com/archives/downloadv2"
    archive_ids = get_archive_ids(feed_id, date)

    # Convert date to YYYYMMDD format for directory name
    date_obj = datetime.datetime.strptime(date, "%Y/%m/%d")
    date_dir_name = date_obj.strftime("%Y%m%d")

    os.makedirs(f"{output_dir}/{feed_id}", exist_ok=True)
    os.makedirs(f"{output_dir}/{feed_id}/{date_dir_name}", exist_ok=True)

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = []
        for id in archive_ids:
            url_date = date_obj.strftime("%Y%m%d")
            url = f"{base_download_url}/{feed_id}/{url_date}/{id}"
            current_output_dir = f"{output_dir}/{feed_id}/{date_dir_name}"
            futures.append(executor.submit(download_mp3, url, current_output_dir, user_agent, login_cookie))

        for future in track(as_completed(futures), total=len(futures), description=f"{date}:"):
            pass

    if combine:
        combine_mp3_files(f"{output_dir}/{feed_id}/{date_dir_name}", feed_id, date)
    
    # Remove the transcription from here
    # if transcribe:
    #     transcribe_audio(f"{output_dir}/{feed_id}/{date_dir_name}", gpu, model_size)


def download_mp3(url, output_dir, user_agent, login_cookie):

    user_agent = str(user_agent)

    headers = {
        "user-agent": user_agent,
        "cookie": login_cookie
    }

    response = requests.get(url, headers=headers, stream=True)

    file_name = response.url.split("/")[-1]
    output_path = os.path.join(output_dir, file_name)


    if response.status_code == 200:

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(1024):
                f.write(chunk)


    else:
        print("Failed to download mp3")
        print(response.text)


def get_archive_ids(feedId, date):
    base_url = 'https://www.broadcastify.com/archives/ajax.php'

    url_date = datetime.datetime.strptime(date, "%Y/%m/%d").strftime("%m/%d/%Y")
    query_params = f"feedId={feedId}&date={url_date}"
    full_url = f"{base_url}?{query_params}" 


    headers = get_urser_agent()
    res = requests.get(full_url, headers=headers)
    dict_res = json.loads(res.text)
    file_names = [f"{i[0]}" for i in dict_res['data']]

    return file_names


def get_urser_agent():
    num_var = random.randint(100, 1000)
    num_var3 = random.randint(10, 100)
    num_var2 = num_var3 % 10
    num_var4 = random.randint(100, 1000)

    user_agent = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/{num_var4}.36 (KHTML, like Gecko) "
                        f"Chrome/58.0.{num_var2}.3029.{num_var} Safari/537.{num_var3}"
    }

    return user_agent


def get_login_cookie(user_agent):

    # load loagin cookie from cookies.json if it exists
    # otherwise get the login cookie and save it to cookies.json 
    if os.path.exists("cookies.json"):
        with open("cookies.json", encoding='utf-8', errors='ignore') as f:
            cookies = json.load(f)
        return f"bcfyuser1={cookies['bcfyuser1']}" 

    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")

    user_agent = str(user_agent)

    url = "https://www.broadcastify.com/login/"
    headers = {
        "user-agent": user_agent, 
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.5",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.broadcastify.com",
        "dnt": "1",
        "referer": "https://www.broadcastify.com/login/",
        "upgrade-insecure-requests": "1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "te": "trailers"
    }
    data = {
        "username": username,
        "password": password,
        "action": "auth",
        "redirect": "https://www.broadcastify.com"
    }

    response = requests.post(url, headers=headers, data=data, allow_redirects=False)

    if response.status_code == 302:
        cookies = response.headers.get("set-cookie")
        if cookies:
            match = re.search(r"bcfyuser1=([^;]+)", cookies)
            if match:
                bcfyuser1 = match.group(1)

                with open("cookies.json", "w") as f:
                    json.dump({"bcfyuser1": bcfyuser1}, f)
                return bcfyuser1
        else:
            print("No cookies found")
            exit(1)

    return None


def combine_mp3_files(directory, feed_id, date):
    ffmpeg_path = which("ffmpeg")
    if ffmpeg_path is None:
        console.print("[red]Error:[/red] FFmpeg not found. Please install FFmpeg and add it to your system PATH.")
        return

    date_obj = datetime.datetime.strptime(date, "%Y/%m/%d")
    date_str = date_obj.strftime("%Y%m%d")

    mp3_files = sorted([f for f in glob.glob(f"{directory}/*.mp3") if not os.path.basename(f).startswith("combined_")])
    console.print(f"Found {len(mp3_files)} MP3 files to combine.")

    if not mp3_files:
        console.print("[yellow]Warning:[/yellow] No MP3 files found to combine.")
        return

    output_file = f"{directory}/combined_{feed_id}_{date_str}.mp3"
    
    # Create a temporary file with the list of input files
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
        for file in mp3_files:
            temp_file.write(f"file '{file}'\n")
        temp_file_name = temp_file.name

    try:
        with console.status(f"Combining MP3 files to {output_file}..."):
            # Use ffmpeg-python to combine the files
            (
                ffmpeg
                .input(temp_file_name, format='concat', safe=0)
                .output(output_file, c='copy')
                .overwrite_output()
                .run(quiet=True, capture_stdout=True, capture_stderr=True)
            )
        console.print(f"Combined MP3 saved to: {output_file}")

        # Remove individual MP3 files
        for mp3_file in mp3_files:
            os.remove(mp3_file)
        console.print("Removed individual MP3 files.")

        # Copy combined file to feed directory
        feed_directory = os.path.dirname(directory)
        destination_file = os.path.join(feed_directory, f"combined_{feed_id}_{date_str}.mp3")
        shutil.copy2(output_file, destination_file)
        console.print(f"Copied combined MP3 to: {destination_file}")

    except ffmpeg.Error as e:
        console.print(f"[red]Error:[/red] FFmpeg encountered an error: {e.stderr.decode()}")
    finally:
        # Clean up the temporary file
        os.unlink(temp_file_name)


def transcribe_audio(directory, use_gpu=False, model_size="distil-large-v3"):
    transcript_dir = f"{directory}/transcripts"
    os.makedirs(transcript_dir, exist_ok=True)

    mp3_files = sorted(glob.glob(f"{directory}/*.mp3"))
    
    if use_gpu:
        device = "cuda"
        compute_type = "float16"
    else:
        device = "cpu"
        compute_type = "int8"
    
    whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)
    
    # Initialize the diarization pipeline
    diarization_pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=os.getenv("HUGGINGFACE_TOKEN"))

    for audio_file in track(mp3_files, description="Transcribing and diarizing audio"):
        # Perform diarization
        diarization = diarization_pipeline(audio_file)
        
        # Perform transcription
        segments, info = whisper_model.transcribe(
            audio_file, 
            beam_size=5, 
            condition_on_previous_text=False, 
            language="en", 
            no_speech_threshold=2, 
            initial_prompt="you are listening to police scanner radio traffic"
        )

        transcript_json_fname = Path(audio_file).stem + ".json"
        transcript_json_path = f"{transcript_dir}/{transcript_json_fname}"

        transcript_txt_fname = Path(audio_file).stem + ".txt"
        transcript_txt_path = f"{transcript_dir}/{transcript_txt_fname}"

        # Prepare JSON output
        output_json = {
            "text": "",
            "segments": []
        }

        # Prepare TXT output
        output_txt = ""

        # Map diarization segments to transcription segments
        diarization_segments = list(diarization.itersegments())
        speaker_map = {}
        speaker_counter = 0

        for segment in segments:
            # Find the corresponding diarization speaker
            overlap = None
            for dia_segment in diarization_segments:
                if (segment.start >= dia_segment.start and segment.start < dia_segment.end) or \
                   (segment.end > dia_segment.start and segment.end <= dia_segment.end) or \
                   (segment.start <= dia_segment.start and segment.end >= dia_segment.end):
                    overlap = dia_segment
                    break
            
            if overlap:
                speaker = overlap.get_label()
            else:
                speaker = "SPEAKER_UNK"

            # Assign a unique speaker ID
            if speaker not in speaker_map:
                speaker_map[speaker] = f"SPEAKER_{speaker_counter:02d}"
                speaker_counter += 1

            speaker_id = speaker_map[speaker]

            # Format timestamp
            start_time = str(datetime.timedelta(seconds=int(segment.start)))
            start_time_formatted = f"[{start_time}]"

            # Append to JSON output
            output_json["text"] += segment.text + " "
            output_json["segments"].append({
                "text": segment.text,
                "start": segment.start,
                "end": segment.end,
                "seek": segment.seek,
                "speaker": speaker_id
            })

            # Append to TXT output
            if not output_txt.endswith(f"{speaker_id}\n"):
                output_txt += f"{speaker_id}\n"
            output_txt += f"{start_time_formatted} - {segment.text}\n"

        # Save JSON transcript
        with open(transcript_json_path, "w", encoding="utf-8") as f_json:
            json.dump(output_json, f_json, indent=4)

        # Save TXT transcript
        with open(transcript_txt_path, "w", encoding="utf-8") as f_txt:
            f_txt.write(output_txt)


def download_past_n_days(feed_id, past_days, output_dir, user_agent, login_cookie, combine, transcribe, jobs, gpu, model_size):
    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=past_days)

    current_date = end_date
    directories_to_transcribe = []

    while current_date >= start_date:
        date_str = current_date.strftime("%Y/%m/%d")
        date_dir_name = current_date.strftime("%Y%m%d")
        console.print(f"Downloading archives for {date_str}")
        
        current_output_dir = f"{output_dir}/{feed_id}/{date_dir_name}"
        download_archive_by_date(feed_id, date_str, output_dir, user_agent, login_cookie, combine, False, jobs, gpu, model_size)
        
        if transcribe:
            directories_to_transcribe.append(current_output_dir)
        
        current_date -= datetime.timedelta(days=1)

    console.print(f"Download complete for the past {past_days} days: {output_dir}/{feed_id}")

    if transcribe:
        for directory in directories_to_transcribe:
            console.print(f"Transcribing audio in {directory}")
            transcribe_audio(directory, gpu, model_size)

    console.print("All operations completed.")

