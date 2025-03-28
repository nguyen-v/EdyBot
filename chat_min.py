import tkinter as tk
from tkinter import scrolledtext, messagebox, Canvas, Frame, Scrollbar
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
    exit()
# --- End Configuration ---

# --- Styling Constants ---
BASE_FONT_SIZE = 12 # << INCREASED FONT SIZE
APP_FONT = ('Helvetica', BASE_FONT_SIZE)
APP_FONT_BOLD = ('Helvetica', BASE_FONT_SIZE, 'bold')

BUBBLE_PADX = 10 # Increased padding slightly
BUBBLE_PADY = 5
BUBBLE_MARGIN_X = 10
BUBBLE_MARGIN_Y = 5
USER_BUBBLE_COLOR = "#007bff" # Blue
BOT_BUBBLE_COLOR = "#e0e0e0" # Light Gray
USER_TEXT_COLOR = "white"
BOT_TEXT_COLOR = "black"
MAX_BUBBLE_WIDTH_FACTOR = 0.7


class MinimalChatApp:
    def __init__(self, root, initial_chat_session, client_ref, model_id_ref, safety_settings_ref):
        self.root = root
        self.chat_session = initial_chat_session
        self.client = client_ref
        self.model_id = model_id_ref
        self.safety_settings = safety_settings_ref
        self.root.title(f"Minimal EdyBot ({self.model_id} - Tkinter)")
        self.root.geometry("1100x700") # Slightly larger default window

        # --- State ---
        self.attached_image_pil = None
        self.image_sent_with_last_api_call = None
        self.latest_frame_cv = None
        self.camera_running = True
        self.frame_queue = queue.Queue(maxsize=5)
        self.response_queue = queue.Queue()
        self._chat_image_references = []

        # --- Layout ---
        self._setup_ui()

        # --- Initialization ---
        self.start_camera_thread()
        self.update_camera_feed()
        self.process_response_queue()
        self.load_initial_prompt()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.chat_canvas.bind("<Configure>", self._on_canvas_configure)

    def _setup_ui(self):
        """Creates and arranges the UI elements."""
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = tk.Frame(main_frame, width=700) # Adjusted initial width guess
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        right_frame = tk.Frame(main_frame, width=400)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 0))

        # --- Pack items within left_frame ---

        # 1. Input area (BOTTOM)
        input_frame = tk.Frame(left_frame)
        input_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(5,0))
        self.message_entry = tk.Entry(input_frame, width=50, font=APP_FONT) # Apply font
        self.message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.message_entry.bind("<Return>", self.send_message)
        self.send_button = tk.Button(input_frame, text="Send", command=self.send_message, font=APP_FONT) # Apply font
        self.send_button.pack(side=tk.RIGHT)

        # 2. Thumbnail preview (BOTTOM, above input)
        self.thumbnail_label = tk.Label(left_frame, text="No image attached", font=APP_FONT) # Apply font
        self.thumbnail_label.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 0))

        # 3. Chat Area (TOP, fills remaining space)
        chat_area_frame = tk.Frame(left_frame)
        chat_area_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.chat_canvas = tk.Canvas(chat_area_frame, borderwidth=0, background="#ffffff")
        self.chat_scrollbar = tk.Scrollbar(chat_area_frame, orient="vertical", command=self.chat_canvas.yview)
        self.scrollable_frame = tk.Frame(self.chat_canvas, background="#ffffff")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.chat_canvas.configure(
                scrollregion=self.chat_canvas.bbox("all")
            )
        )
        self.canvas_frame_id = self.chat_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.chat_canvas.configure(yscrollcommand=self.chat_scrollbar.set)

        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_scrollbar.pack(side="right", fill="y")

        self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.chat_canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.chat_canvas.bind_all("<Button-5>", self._on_mousewheel)

        # --- Pack items within right_frame ---
        self.camera_label = tk.Label(right_frame, text="Starting Camera...", bg='grey', width=40, height=20, font=APP_FONT) # Apply font
        self.camera_label.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        camera_buttons_frame = tk.Frame(right_frame)
        camera_buttons_frame.pack(fill=tk.X)
        self.attach_button = tk.Button(camera_buttons_frame, text="Attach Picture", command=self.attach_image, font=APP_FONT) # Apply font
        self.attach_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 0))


    def _on_canvas_configure(self, event=None):
         """Reset the canvas window dimensions when canvas resizes."""
         if event:
             self.chat_canvas.itemconfig(self.canvas_frame_id, width=event.width)

    def _on_mousewheel(self, event):
        """Handle mouse wheel scrolling for the chat canvas."""
        if event.num == 4 or event.delta > 0:
            self.chat_canvas.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:
            self.chat_canvas.yview_scroll(1, "units")

    def _scroll_to_bottom(self):
        """Scrolls the chat canvas to the very bottom."""
        self.chat_canvas.update_idletasks()
        self.chat_canvas.yview_moveto(1.0)

    # --- Camera Handling ---
    def _camera_thread_func(self):
        """Function run in the camera thread to capture frames."""
        cap = None
        try:
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened():
                self.response_queue.put("SYSTEM_ERROR: Could not open camera.")
                return

            while self.camera_running:
                ret, frame = cap.read()
                if ret:
                    self.latest_frame_cv = frame.copy()
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    try:
                        self.frame_queue.put_nowait(rgb_frame)
                    except queue.Full:
                        try:
                            self.frame_queue.get_nowait()
                            self.frame_queue.put_nowait(rgb_frame)
                        except queue.Empty:
                            pass
                else:
                    self.response_queue.put("SYSTEM_ERROR: Failed to read frame.")
                    time.sleep(1)

                time.sleep(0.03)
        except Exception as e:
            self.response_queue.put(f"SYSTEM_ERROR: Camera Thread Error: {e}")
        finally:
            if cap and cap.isOpened():
                cap.release()
            print("Camera thread stopped.")

    def start_camera_thread(self):
        """Starts the camera thread."""
        self.camera_thread = threading.Thread(target=self._camera_thread_func, daemon=True)
        self.camera_thread.start()

    def update_camera_feed(self):
        """Periodically checks frame queue and updates camera label in UI."""
        try:
            while not self.frame_queue.empty():
                rgb_frame = self.frame_queue.get_nowait()
                img = Image.fromarray(rgb_frame)
                label_w = self.camera_label.winfo_width()
                label_h = self.camera_label.winfo_height()
                if label_w > 1 and label_h > 1:
                    img.thumbnail((label_w, label_h), Image.Resampling.LANCZOS)

                imgtk = ImageTk.PhotoImage(image=img)
                self.camera_label.imgtk = imgtk
                self.camera_label.config(image=imgtk, width=imgtk.width(), height=imgtk.height())
        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error updating camera feed: {e}")
            self.camera_label.config(image='', text=f"Feed Error:\n{e}")

        if self.camera_running:
            self.root.after(30, self.update_camera_feed)

    # --- Chat Display ---
    # *** MODIFIED: Add "Edy" label for bot messages, apply APP_FONT ***
    def add_message(self, sender, message, img_pil=None):
        """Adds a message bubble (Frame with Label) to the scrollable chat area."""
        if sender == "System":
            print(f"System: {message}")
            return

        align_anchor = 'w' if sender == "Edy" else 'e'
        bubble_color = BOT_BUBBLE_COLOR if sender == "Edy" else USER_BUBBLE_COLOR
        text_color = BOT_TEXT_COLOR if sender == "Edy" else USER_TEXT_COLOR
        label_justify = tk.LEFT if sender == "Edy" else tk.RIGHT

        # Outer frame for alignment and margins
        outer_frame = tk.Frame(self.scrollable_frame, bg=self.scrollable_frame.cget('bg'))
        outer_frame.pack(fill='x', padx=BUBBLE_MARGIN_X, pady=BUBBLE_MARGIN_Y, anchor=align_anchor)

        # *** Add "Edy" label ONLY for bot messages ***
        if sender == "Edy":
            edy_label = tk.Label(
                outer_frame,
                text="Edy",
                font=APP_FONT_BOLD, # Bold font for name
                fg=BOT_TEXT_COLOR, # Use bot text color or a distinct color
                bg=self.scrollable_frame.cget('bg') # Match background
            )
            # Pack name label above the bubble, aligned left
            edy_label.pack(anchor='w', pady=(0, 2)) # Small padding below name

        # Inner frame is the visual bubble
        bubble_frame = tk.Frame(outer_frame, bg=bubble_color)
        bubble_frame.pack(anchor=align_anchor)

        canvas_width = self.chat_canvas.winfo_width()
        if canvas_width <= 1:
            canvas_width = 600 # Adjusted estimate
        wrap_width = int(canvas_width * MAX_BUBBLE_WIDTH_FACTOR)

        imgtk = None
        compound_pos = tk.NONE

        if img_pil:
            try:
                img_copy = img_pil.copy()
                img_copy.thumbnail((200, 200), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(img_copy)
                self._chat_image_references.append(imgtk)
                compound_pos = tk.TOP
            except Exception as e:
                print(f"Error processing image for chat bubble: {e}")
                imgtk = None

        # Create the main content Label inside the bubble frame
        bubble_label = tk.Label(
            bubble_frame,
            text=message if message else "",
            font=APP_FONT, # Apply base font
            fg=text_color,
            bg=bubble_color,
            justify=label_justify,
            wraplength=wrap_width,
            padx=BUBBLE_PADX,
            pady=BUBBLE_PADY,
            image=imgtk,
            compound=compound_pos
        )
        bubble_label.pack(fill='both', expand=True)

        self.root.after(50, self._scroll_to_bottom)


    # --- Image Attachment ---
    def attach_image(self):
        """Captures current camera frame and updates thumbnail."""
        if self.latest_frame_cv is not None:
            try:
                rgb_frame = cv2.cvtColor(self.latest_frame_cv, cv2.COLOR_BGR2RGB)
                self.attached_image_pil = Image.fromarray(rgb_frame)
                print("Image attached by user.")

                img_copy = self.attached_image_pil.copy()
                img_copy.thumbnail((150, 100), Image.Resampling.LANCZOS)
                imgtk = ImageTk.PhotoImage(img_copy)
                self.thumbnail_label.imgtk = imgtk
                # Also update font here if needed, though text changes
                self.thumbnail_label.config(
                    image=imgtk,
                    text=f"Attached ({self.attached_image_pil.width}x{self.attached_image_pil.height})",
                    font=APP_FONT # Ensure font is reapplied
                )
            except Exception as e:
                print(f"System: Error attaching image: {e}")
                self._clear_attachment_state()
        else:
            print("System: No camera frame available to attach.")

    def _clear_attachment_state(self):
        """Resets the currently attached image state and thumbnail."""
        self.attached_image_pil = None
        self.thumbnail_label.config(image='', text="No image attached", font=APP_FONT) # Reset font too


    # --- Gemini Interaction ---
    def _send_to_gemini_thread_func(self, parts):
        """Sends message parts to Gemini API in a separate thread."""
        try:
            response = self.chat_session.send_message(message=parts)
            response_text = response.text
        except Exception as e:
            response_text = f"SYSTEM_ERROR: Gemini API Error: {e}"
            print(f"Gemini API Error: {e}")
        self.response_queue.put(response_text)

    def process_response_queue(self):
        """Checks queue for Gemini responses or errors and updates UI/logs."""
        try:
            response_text = self.response_queue.get_nowait()

            if response_text.startswith("SYSTEM_ERROR:"):
                print(f"System: {response_text.replace('SYSTEM_ERROR:', '').strip()}")
            else:
                self.add_message("Edy", response_text) # Add bot bubble

                if "ED, ED, EDY" in response_text:
                    print("Password 'ED, ED, EDY' detected in response!")
                    print("System: Password detected! Initiating print and reset sequence...")

                    image_to_print_now = self.image_sent_with_last_api_call
                    if image_to_print_now:
                        self.root.after(100, lambda img=image_to_print_now: self.print_image(image_override=img))
                    else:
                        print("System: Password detected, but no image was sent with the triggering message.")

                    self.root.after(15000, self.reset_chat)
                    self.image_sent_with_last_api_call = None

        except queue.Empty:
            pass
        except Exception as e:
            print(f"Error processing response queue: {e}")

        self.root.after(100, self.process_response_queue)

    def send_message(self, event=None):
        """Prepares and sends user message (text/image) to Gemini."""
        user_text = self.message_entry.get().strip()
        current_attached_image = self.attached_image_pil

        if not user_text and current_attached_image is None:
            return

        message_parts = []
        display_img_copy = current_attached_image.copy() if current_attached_image else None
        self.add_message("You", user_text, img_pil=display_img_copy) # Add user bubble

        self.image_sent_with_last_api_call = None # Reset context

        if current_attached_image:
            try:
                img_byte_arr = io.BytesIO()
                rgb_image = current_attached_image
                if rgb_image.mode != 'RGB':
                    rgb_image = rgb_image.convert('RGB')
                rgb_image.save(img_byte_arr, format='JPEG', quality=90)
                img_bytes = img_byte_arr.getvalue()
                message_parts.append(Part.from_bytes(img_bytes, mime_type="image/jpeg"))
                self.image_sent_with_last_api_call = current_attached_image.copy()
                print(f"Image part prepared ({len(img_bytes)} bytes). Context stored.")
            except Exception as e:
                print(f"System: Error processing image for sending: {e}")
                self._clear_attachment_state()
                self.image_sent_with_last_api_call = None
                return

        if user_text:
            message_parts.append(Part(text=user_text))
            print(f"Text part prepared: '{user_text}'")

        self.message_entry.delete(0, tk.END)
        self._clear_attachment_state()

        if message_parts:
            api_thread = threading.Thread(target=self._send_to_gemini_thread_func, args=(message_parts,), daemon=True)
            api_thread.start()
        else:
            print("Warning: No valid parts generated to send to API.")

    # --- Printing ---
    def print_image(self, image_override):
        """Prints the provided image using Windows default printer."""
        image_to_print = image_override
        if image_to_print is None:
            print("Print Error: No image provided to print function.")
            return

        print(f"Starting print process for image (id: {id(image_to_print)})...")
        temp_path = None
        hDC = None
        try:
            printer_name = win32print.GetDefaultPrinter()
            if not printer_name:
                print("Print Error: No default printer found.")
                return
            print(f"Using printer: {printer_name}")

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            photo_path = os.path.join(PHOTOS_DIR, f"print_{timestamp}.jpg")
            try:
                rgb_image_print = image_to_print
                if rgb_image_print.mode != 'RGB':
                    rgb_image_print = rgb_image_print.convert('RGB')
                rgb_image_print.save(photo_path, quality=95)
                print(f"Image saved for record: {photo_path}")
            except Exception as save_err:
                print(f"Warning: Could not save print copy: {save_err}")

            temp_file = tempfile.NamedTemporaryFile(suffix='.bmp', delete=False)
            temp_path = temp_file.name
            temp_file.close()
            rgb_image_print_bmp = image_to_print
            if rgb_image_print_bmp.mode != 'RGB':
                rgb_image_print_bmp = rgb_image_print_bmp.convert('RGB')
            rgb_image_print_bmp.save(temp_path, format='BMP')
            print(f"Using temporary file: {temp_path}")

            hDC = win32ui.CreateDC()
            hDC.CreatePrinterDC(printer_name)
            print("Printer DC created.")

            PHYSICALWIDTH=110
            PHYSICALHEIGHT=111
            printer_width_px=hDC.GetDeviceCaps(PHYSICALWIDTH)
            printer_height_px=hDC.GetDeviceCaps(PHYSICALHEIGHT)

            bmp = Image.open(temp_path)

            img_w, img_h = bmp.size
            aspect = img_w / img_h
            scaled_w = printer_width_px
            scaled_h = int(scaled_w / aspect)
            if scaled_h > printer_height_px:
                scaled_h = printer_height_px
                scaled_w = int(scaled_h * aspect)
            x_offset = (printer_width_px - scaled_w) // 2
            y_offset = (printer_height_px - scaled_h) // 2

            hDC.StartDoc(f"EdyBot Print {timestamp}")
            hDC.StartPage()
            dib = ImageWin.Dib(bmp)
            dib.draw(hDC.GetHandleOutput(), (x_offset, y_offset, x_offset + scaled_w, y_offset + scaled_h))
            hDC.EndPage()
            hDC.EndDoc()
            print("Print job sent.")

        except Exception as e:
            print(f"ERROR during printing: {e}")
        finally:
            # Cleanup
            if hDC:
                try:
                    hDC.DeleteDC()
                    print("Printer DC deleted.")
                except Exception as dc_err:
                    print(f"Error deleting DC: {dc_err}")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                    print(f"Temporary file deleted: {temp_path}")
                except Exception as del_err:
                    print(f"Warning: Could not delete temp file {temp_path}: {del_err}")

    # --- Chat Reset ---
    def reset_chat(self):
        """Resets chat display, attachment state, and Gemini session."""
        print("Resetting chat...")
        self._clear_attachment_state()
        self.image_sent_with_last_api_call = None
        self._chat_image_references.clear()

        # Destroy all existing bubble frames in the scrollable area
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        self.chat_canvas.update_idletasks()
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        self.chat_canvas.yview_moveto(0.0)

        try:
            self.chat_session = create_gemini_chat(self.model_id, self.safety_settings)
            print("New Gemini chat session created.")
            self.root.after(200, self.load_initial_prompt)
        except Exception as e:
            print(f"ERROR: Failed to reset Gemini Chat Session: {e}")

    # --- Initial Prompt Load ---
    def load_initial_prompt(self):
        """Loads initial prompt from file and sends to current session."""
        try:
            prompt_file="initial_prompt.txt"
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding='utf-8') as f:
                    initial_prompt = f.read().strip()
                if initial_prompt:
                    print("Sending initial prompt...")
                    api_thread = threading.Thread(
                        target=self._send_to_gemini_thread_func,
                        args=([Part(text=initial_prompt)],),
                        daemon=True
                    )
                    api_thread.start()
                else:
                    print("System: initial_prompt.txt is empty.")
            else:
                print("System: initial_prompt.txt not found.")
        except Exception as e:
            print(f"System: Error loading initial prompt: {e}")

    # --- Closing ---
    def on_closing(self):
        """Handles window close event."""
        print("Closing application...")
        self.camera_running = False
        self._chat_image_references.clear()
        if hasattr(self.thumbnail_label, 'imgtk'):
            del self.thumbnail_label.imgtk
        if hasattr(self.camera_label, 'imgtk'):
            del self.camera_label.imgtk
        time.sleep(0.2)
        self.root.destroy()


# --- Main Execution ---
if __name__ == "__main__":
    if all(var in globals() for var in ['client', 'MODEL_ID', 'safety_settings', 'chat_session']):
        root = tk.Tk()
        app = MinimalChatApp(root, chat_session, client, MODEL_ID, safety_settings)
        root.mainloop()
    else:
        print("Critical Error: Global configuration or Gemini session failed during startup. Exiting.")
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror("Startup Error", "Gemini client/session failed. Check console for details.")
        error_root.destroy()