import tkinter as tk
from tkinter import scrolledtext, messagebox
import cv2
import threading
import queue
import os
from google import genai
from google.genai.types import Part, GenerateContentConfig, SafetySetting
from dotenv import load_dotenv
from PIL import Image, ImageTk, ImageWin
import time
import win32print
import win32ui
import tempfile
import io
from datetime import datetime

# --- Configuration ---
PHOTOS_DIR = "photos_tk"
if not os.path.exists(PHOTOS_DIR):
    os.makedirs(PHOTOS_DIR)

load_dotenv()
API_KEY = os.getenv("GOOGLE_API_KEY")
if not API_KEY:
    print("ERROR: GOOGLE_API_KEY environment variable not set.")
    exit()

try:
    client = genai.Client(api_key=API_KEY)
    MODEL_ID = "gemini-2.0-flash-exp"
    safety_settings = [
        SafetySetting(category=f"HARM_CATEGORY_{c}", threshold="BLOCK_NONE")
        for c in ["DANGEROUS_CONTENT", "HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT"]
    ]

    def create_gemini_chat(model_id_to_use, settings):
        """Helper function to create a new Gemini chat session."""
        return client.chats.create(
            model=model_id_to_use,
            config=GenerateContentConfig(safety_settings=settings)
        )

    chat_session = create_gemini_chat(MODEL_ID, safety_settings)
    print(f"Gemini Client and Chat Session ({MODEL_ID}) initialized.")

except Exception as e:
    print(f"ERROR: Failed to initialize Gemini Client/Chat: {e}")
    # Avoid showing messagebox if GUI hasn't started
    exit()
# --- End Configuration ---


class MinimalChatApp:
    def __init__(self, root, initial_chat_session, client_ref, model_id_ref, safety_settings_ref):
        self.root = root
        self.chat_session = initial_chat_session
        self.client = client_ref
        self.model_id = model_id_ref
        self.safety_settings = safety_settings_ref
        self.root.title(f"Minimal EdyBot ({self.model_id} - Tkinter)")
        self.root.geometry("1000x600")

        # --- State ---
        self.attached_image_pil = None # Current image selected by user
        self.image_sent_with_last_api_call = None # Store image associated with the API call
        self.latest_frame_cv = None
        self.camera_running = True
        self.frame_queue = queue.Queue(maxsize=5)
        self.response_queue = queue.Queue()

        # --- Layout ---
        self._setup_ui()

        # --- Initialization ---
        self.start_camera_thread()
        self.update_camera_feed()
        self.process_response_queue()
        self.load_initial_prompt()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _setup_ui(self):
        """Creates and arranges the UI elements."""
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Left side (Chat Area)
        left_frame = tk.Frame(main_frame, width=600)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        self.chat_display = scrolledtext.ScrolledText(left_frame, wrap=tk.WORD, state='disabled', height=20)
        self.chat_display.pack(fill=tk.BOTH, expand=True, pady=(0, 5))
        # Configure tags for message styling
        self.chat_display.tag_configure('user', foreground='blue', justify='right')
        self.chat_display.tag_configure('bot', foreground='green', justify='left')
        # 'image' tag exists mainly as placeholder for image_create, justification is handled by 'user'/'bot' tags
        self.chat_display.tag_configure('image')

        self.thumbnail_label = tk.Label(left_frame, text="No image attached")
        self.thumbnail_label.pack(fill=tk.X, pady=(0, 5))

        input_frame = tk.Frame(left_frame)
        input_frame.pack(fill=tk.X)
        self.message_entry = tk.Entry(input_frame, width=50)
        self.message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.message_entry.bind("<Return>", self.send_message) # Bind Enter key
        self.send_button = tk.Button(input_frame, text="Send", command=self.send_message)
        self.send_button.pack(side=tk.RIGHT)

        # Right side (Camera Feed and Controls)
        right_frame = tk.Frame(main_frame, width=400)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 0))

        self.camera_label = tk.Label(right_frame, text="Starting Camera...", bg='grey', width=40, height=20)
        self.camera_label.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        camera_buttons_frame = tk.Frame(right_frame)
        camera_buttons_frame.pack(fill=tk.X)
        self.attach_button = tk.Button(camera_buttons_frame, text="Attach Picture", command=self.attach_image)
        self.attach_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 0)) # Attach button fills width

    # --- Camera Handling ---
    def _camera_thread_func(self):
        """Function run in the camera thread to capture frames."""
        cap = None
        try:
            # Try DirectShow backend for better compatibility on Windows
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                self.response_queue.put("SYSTEM_ERROR: Could not open camera.")
                return

            while self.camera_running:
                ret, frame = cap.read()
                if ret:
                    self.latest_frame_cv = frame.copy() # Store for capture
                    # Convert for display (Tkinter uses RGB)
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    # Put frame in queue for main thread, handle queue full
                    try:
                        self.frame_queue.put_nowait(rgb_frame)
                    except queue.Full:
                        try:
                            self.frame_queue.get_nowait() # Discard oldest
                            self.frame_queue.put_nowait(rgb_frame) # Retry putting
                        except queue.Empty:
                            pass # Should not happen concurrently
                else:
                    # Report error if frame reading fails
                    self.response_queue.put("SYSTEM_ERROR: Failed to read frame.")
                    time.sleep(1) # Avoid busy-looping

                # Control frame rate (~30 FPS)
                time.sleep(0.03)
        except Exception as e:
            # Report any unexpected errors from the camera thread
            self.response_queue.put(f"SYSTEM_ERROR: Camera Thread Error: {e}")
        finally:
            # Ensure camera is released
            if cap and cap.isOpened():
                cap.release()
            print("Camera thread stopped.") # Log thread exit

    def start_camera_thread(self):
        """Starts the camera thread."""
        self.camera_thread = threading.Thread(target=self._camera_thread_func, daemon=True)
        self.camera_thread.start()

    def update_camera_feed(self):
        """Periodically checks frame queue and updates camera label in UI."""
        try:
            # Process all frames currently in the queue
            while not self.frame_queue.empty():
                rgb_frame = self.frame_queue.get_nowait()
                img = Image.fromarray(rgb_frame)

                # Resize image to fit the label dimensions while maintaining aspect ratio
                label_w = self.camera_label.winfo_width()
                label_h = self.camera_label.winfo_height()
                # Avoid division by zero or resizing before label is drawn
                if label_w > 1 and label_h > 1:
                    img.thumbnail((label_w, label_h), Image.Resampling.LANCZOS)

                # Convert PIL image to Tkinter PhotoImage
                imgtk = ImageTk.PhotoImage(image=img)
                # Keep a reference to prevent garbage collection!
                self.camera_label.imgtk = imgtk
                # Update the label content and size
                self.camera_label.config(image=imgtk, width=imgtk.width(), height=imgtk.height())

        except queue.Empty:
            pass # Normal case: no new frame
        except Exception as e:
            # Log error and display indicator on the label
            print(f"Error updating camera feed: {e}")
            self.camera_label.config(image='', text=f"Feed Error:\n{e}")

        # Schedule the next check if the camera should still be running
        if self.camera_running:
            self.root.after(30, self.update_camera_feed) # Check roughly 30 times/sec

    # --- Chat Display ---
    def add_message(self, sender, message, img_pil=None):
        """Adds a message (text and/or image) to the chat display, handling alignment."""
        # Redirect System messages to console log
        if sender == "System":
            print(f"System: {message}")
            return

        # Enable text widget for modification
        self.chat_display.config(state='normal')
        try:
            if sender == "You":
                tag = 'user' # Tag for styling and justification
                prefix = "You: "
                # Mark the starting position BEFORE inserting anything for this message
                start_index = self.chat_display.index(tk.END + "-1c") # Get position just before newline

                # Insert text content (if any)
                if message:
                    text_to_insert = f"{prefix}{message}"
                    # Add a space separator if an image will follow the text
                    if img_pil:
                        text_to_insert += " "
                    self.chat_display.insert(tk.END, text_to_insert)

                # Insert image widget (if any) AFTER text on the same conceptual line
                if img_pil:
                    img_copy = img_pil.copy()
                    img_copy.thumbnail((150, 150), Image.Resampling.LANCZOS) # Small chat thumbnail
                    imgtk = ImageTk.PhotoImage(img_copy)
                    img_ref_name = f'img_{time.time()}' # Unique name for the image instance
                    # Store reference to prevent garbage collection by Tkinter
                    if not hasattr(self.chat_display, 'image_references'):
                         self.chat_display.image_references = {}
                    self.chat_display.image_references[img_ref_name] = imgtk
                    # Embed the image in the text widget
                    self.chat_display.image_create(tk.END, image=imgtk, name=img_ref_name)

                # Insert the final newline character for this message block
                self.chat_display.insert(tk.END, '\n')
                # Mark the ending position AFTER inserting everything including the newline
                end_index = self.chat_display.index(tk.END + "-1c")

                # Apply the 'user' tag (with justify='right') to the entire block just inserted
                self.chat_display.tag_add(tag, start_index, end_index)

            elif sender == "Edy":
                tag = 'bot' # Tag for styling and justification
                prefix = "Edy: "
                # For bot, just insert text and newline with the tag
                start_index = self.chat_display.index(tk.END + "-1c")
                self.chat_display.insert(tk.END, f"{prefix}{message}\n")
                end_index = self.chat_display.index(tk.END + "-1c")
                # Apply the 'bot' tag to the entire block
                self.chat_display.tag_add(tag, start_index, end_index)

            # Scroll the chat window to make the latest message visible
            self.chat_display.see(tk.END)
        except Exception as e:
            # Log errors occurring during GUI update
            print(f"Error adding message to GUI display: {e}")
        finally:
            # Disable text widget again to prevent user typing directly
            self.chat_display.config(state='disabled')

    # --- Image Attachment ---
    def attach_image(self):
        """Captures current camera frame and updates thumbnail."""
        if self.latest_frame_cv is not None:
            try:
                rgb_frame = cv2.cvtColor(self.latest_frame_cv, cv2.COLOR_BGR2RGB)
                self.attached_image_pil = Image.fromarray(rgb_frame) # Store as current selection
                print("Image attached by user.") # Log action

                # Update the thumbnail preview
                img_copy = self.attached_image_pil.copy()
                img_copy.thumbnail((150, 100), Image.Resampling.LANCZOS) # Size for thumbnail label
                imgtk = ImageTk.PhotoImage(img_copy)
                self.thumbnail_label.imgtk = imgtk # Keep reference
                self.thumbnail_label.config(image=imgtk,
                                           text=f"Attached ({self.attached_image_pil.width}x{self.attached_image_pil.height})")
            except Exception as e:
                # Log error, don't show in chat UI
                print(f"System: Error attaching image: {e}")
                self._clear_attachment_state() # Reset UI on error
        else:
            # Log if no frame is available
            print("System: No camera frame available to attach.")

    def _clear_attachment_state(self):
        """Resets the currently attached image state and thumbnail."""
        self.attached_image_pil = None
        self.thumbnail_label.config(image='', text="No image attached")

    # --- Gemini Interaction ---
    def _send_to_gemini_thread_func(self, parts):
        """Sends message parts to Gemini API in a separate thread."""
        try:
            response = self.chat_session.send_message(message=parts)
            response_text = response.text
        except Exception as e:
            # Prefix API errors for identification in the queue processor
            response_text = f"SYSTEM_ERROR: Gemini API Error: {e}"
            print(f"Gemini API Error: {e}") # Also log raw error
        # Put result (or error) into the queue for the main thread
        self.response_queue.put(response_text)

    def process_response_queue(self):
        """Checks queue for Gemini responses or thread errors and updates UI."""
        try:
            # Non-blocking check for items in the queue
            response_text = self.response_queue.get_nowait()

            # Handle system errors reported from threads (log to console)
            if response_text.startswith("SYSTEM_ERROR:"):
                print(f"System: {response_text.replace('SYSTEM_ERROR:', '').strip()}")
            else:
                # Add valid Gemini response to chat display
                self.add_message("Edy", response_text)

                # Check for password trigger within the bot's response
                if "ED, ED, EDY" in response_text:
                    print("Password 'ED, ED, EDY' detected in response!") # Log detection
                    print("System: Password detected! Initiating print and reset sequence...") # Log action

                    # Get the image context associated with the API call that triggered this response
                    image_to_print_now = self.image_sent_with_last_api_call

                    if image_to_print_now:
                        # Schedule the print function with the correct image context
                        self.root.after(100, lambda img=image_to_print_now: self.print_image(image_override=img))
                    else:
                        # Log if password found but no image was sent
                        print("System: Password detected, but no image was sent with the triggering message.")

                    # Schedule the chat reset after a delay
                    self.root.after(15000, self.reset_chat)

                    # Clear the context *after* scheduling actions that use it
                    self.image_sent_with_last_api_call = None

        except queue.Empty:
            pass # Normal case: no new response/error
        except Exception as e:
            # Log any errors occurring during queue processing itself
            print(f"Error processing response queue: {e}")

        # Schedule the next check
        self.root.after(100, self.process_response_queue)

    def send_message(self, event=None):
        """Prepares and sends user message (text/image) to Gemini."""
        user_text = self.message_entry.get().strip()
        # Capture the currently selected image at the time of sending
        current_attached_image = self.attached_image_pil

        # Do nothing if there's no text and no image attached
        if not user_text and current_attached_image is None:
            return

        message_parts = [] # List to hold parts for the API call

        # Prepare a copy for display (don't modify the original attached image)
        display_img_copy = current_attached_image.copy() if current_attached_image else None
        # Add user's input (text and/or image) to the chat display immediately
        self.add_message("You", user_text, img_pil=display_img_copy)

        # Reset the context for the *next* potential API call before processing this one
        self.image_sent_with_last_api_call = None

        # Process the image if one was attached when send was triggered
        if current_attached_image:
            try:
                img_byte_arr = io.BytesIO()
                # Ensure image is RGB before saving as JPEG
                rgb_image = current_attached_image
                if rgb_image.mode != 'RGB':
                    rgb_image = rgb_image.convert('RGB')
                rgb_image.save(img_byte_arr, format='JPEG', quality=90)
                img_bytes = img_byte_arr.getvalue()
                message_parts.append(Part.from_bytes(img_bytes, mime_type="image/jpeg"))

                # *** Store a COPY of the image being sent for this specific API call context ***
                self.image_sent_with_last_api_call = current_attached_image.copy()
                print(f"Image part prepared ({len(img_bytes)} bytes). Context stored.") # Log action

            except Exception as e:
                # Log error and prevent sending if image processing fails
                print(f"System: Error processing image for sending: {e}")
                self._clear_attachment_state() # Clear user selection UI on error
                self.image_sent_with_last_api_call = None # Ensure context is cleared too
                return # Stop processing this message

        # Add text part if present
        if user_text:
            message_parts.append(Part(text=user_text))
            print(f"Text part prepared: '{user_text}'") # Log action

        # Clear user input field AND the user attachment state/UI
        # Do this *after* preparing parts and storing context
        self.message_entry.delete(0, tk.END)
        self._clear_attachment_state() # Clears self.attached_image_pil and UI thumbnail

        # Start the API call thread if message parts were created
        if message_parts:
            api_thread = threading.Thread(target=self._send_to_gemini_thread_func, args=(message_parts,), daemon=True)
            api_thread.start()
        else:
            # This condition should ideally not be met if the initial check passed
            print("Warning: No valid parts generated to send to API.")

    # --- Printing ---
    def print_image(self, image_override):
        """Prints the provided image using Windows default printer."""
        # This function is now only called with an explicit image from the password trigger
        image_to_print = image_override

        if image_to_print is None:
            # Should not happen if called correctly, indicates logic error elsewhere
            print("Print Error: No image provided to print function (image_override is None).")
            return

        print(f"Starting print process for image (id: {id(image_to_print)})...") # Log start
        temp_path = None
        hDC = None
        try:
            printer_name = win32print.GetDefaultPrinter()
            if not printer_name:
                print("Print Error: No default printer found.") # Log error
                return
            print(f"Using printer: {printer_name}") # Log info

            # Save a copy for record-keeping purposes
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            photo_path = os.path.join(PHOTOS_DIR, f"print_{timestamp}.jpg")
            try:
                rgb_image_print = image_to_print
                if rgb_image_print.mode != 'RGB':
                    rgb_image_print = rgb_image_print.convert('RGB')
                rgb_image_print.save(photo_path, quality=95)
                print(f"Image saved for record: {photo_path}")
            except Exception as save_err:
                print(f"Warning: Could not save print copy: {save_err}") # Log warning

            # Use a temporary BMP file for ImageWin.Dib compatibility
            temp_file = tempfile.NamedTemporaryFile(suffix='.bmp', delete=False)
            temp_path = temp_file.name
            temp_file.close() # Close handle before saving to it
            rgb_image_print_bmp = image_to_print
            if rgb_image_print_bmp.mode != 'RGB':
                rgb_image_print_bmp = rgb_image_print_bmp.convert('RGB')
            rgb_image_print_bmp.save(temp_path, format='BMP')
            print(f"Using temporary file: {temp_path}") # Log temp file path

            # --- Windows Printing Logic ---
            hDC = win32ui.CreateDC()
            hDC.CreatePrinterDC(printer_name)
            print("Printer DC created.") # Log DC creation

            # Get printer dimensions
            PHYSICALWIDTH = 110
            PHYSICALHEIGHT = 111
            printer_width_px = hDC.GetDeviceCaps(PHYSICALWIDTH)
            printer_height_px = hDC.GetDeviceCaps(PHYSICALHEIGHT)

            # Load temporary BMP
            bmp = Image.open(temp_path)

            # Simple scaling to fit page while maintaining aspect ratio
            img_w, img_h = bmp.size
            aspect = img_w / img_h
            scaled_w = printer_width_px
            scaled_h = int(scaled_w / aspect)
            if scaled_h > printer_height_px:
                scaled_h = printer_height_px
                scaled_w = int(scaled_h * aspect)
            # Center image on page
            x_offset = (printer_width_px - scaled_w) // 2
            y_offset = (printer_height_px - scaled_h) // 2

            # Start print job
            hDC.StartDoc(f"EdyBot Print {timestamp}")
            hDC.StartPage()

            # Draw bitmap to printer DC
            dib = ImageWin.Dib(bmp)
            dib.draw(hDC.GetHandleOutput(), (x_offset, y_offset, x_offset + scaled_w, y_offset + scaled_h))

            # End print job
            hDC.EndPage()
            hDC.EndDoc()
            print("Print job sent.") # Log success

        except Exception as e:
            print(f"ERROR during printing: {e}") # Log any printing error
        finally:
            # --- Cleanup ---
            # Delete printer DC
            if hDC:
                try:
                    hDC.DeleteDC()
                    print("Printer DC deleted.")
                except Exception as dc_err:
                    print(f"Error deleting DC: {dc_err}") # Log cleanup error
            # Delete temporary file
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    print(f"Temporary file deleted: {temp_path}")
                except Exception as del_err:
                    # File might be locked by system temporarily
                    print(f"Warning: Could not delete temp file {temp_path}: {del_err}")

    # --- Chat Reset ---
    def reset_chat(self):
        """Resets chat display, attachment state, and Gemini session."""
        print("Resetting chat...") # Log action
        # Clear UI state related to attachments
        self._clear_attachment_state()
        # Clear the image context from the last API call
        self.image_sent_with_last_api_call = None

        # Clear the text display widget
        self.chat_display.config(state='normal')
        self.chat_display.delete('1.0', tk.END)

        try:
            # Create a completely new backend chat session
            self.chat_session = create_gemini_chat(self.model_id, self.safety_settings)
            print("New Gemini chat session created.") # Log success
            # Load the initial prompt into the *new* session after a short delay
            self.root.after(200, self.load_initial_prompt)
        except Exception as e:
            # Log error if session creation fails
            print(f"ERROR: Failed to reset Gemini Chat Session: {e}")
        finally:
            # Disable display again after reset attempt
             self.chat_display.config(state='disabled')

    # --- Initial Prompt Load ---
    def load_initial_prompt(self):
        """Loads initial prompt from file and sends to current session."""
        try:
            prompt_file="initial_prompt.txt"
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding='utf-8') as f:
                    initial_prompt = f.read().strip()
                if initial_prompt:
                    print("Sending initial prompt...") # Log action
                    # Send prompt in background thread
                    api_thread = threading.Thread(
                        target=self._send_to_gemini_thread_func,
                        args=([Part(text=initial_prompt)],),
                        daemon=True
                    )
                    api_thread.start()
                else:
                    # Log if file is empty
                    print("System: initial_prompt.txt is empty.")
            else:
                # Log if file not found
                print("System: initial_prompt.txt not found.")
        except Exception as e:
            # Log any error during file reading/sending setup
            print(f"System: Error loading initial prompt: {e}")

    # --- Closing ---
    def on_closing(self):
        """Handles window close event."""
        print("Closing application...")
        self.camera_running = False # Signal background threads to stop
        # Give threads a moment to potentially finish queue writes/checks
        time.sleep(0.2)
        # Destroy the Tkinter window and exit application
        self.root.destroy()


# --- Main Execution ---
if __name__ == "__main__":
    # Ensure global variables needed by the app class are initialized before creating the app
    if all(var in globals() for var in ['client', 'MODEL_ID', 'safety_settings', 'chat_session']):
        root = tk.Tk()
        # Pass necessary references to the app instance
        app = MinimalChatApp(root, chat_session, client, MODEL_ID, safety_settings)
        root.mainloop() # Start the Tkinter event loop
    else:
        # Handle critical startup error if Gemini session failed
        print("Critical Error: Global configuration or Gemini session failed during startup. Exiting.")
        # Show a simple error popup if UI cannot start
        error_root = tk.Tk()
        error_root.withdraw() # Hide the empty root window
        messagebox.showerror("Startup Error", "Gemini client/session failed. Check console for details.")
        error_root.destroy()