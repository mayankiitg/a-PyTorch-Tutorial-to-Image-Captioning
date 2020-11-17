import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from datasets import *
from utils import *
from nltk.translate.bleu_score import corpus_bleu
import torch.nn.functional as F
from tqdm import tqdm
from nlgeval import NLGEval
import numpy as np

nlgeval = NLGEval(no_skipthoughts=True, no_glove=True)  # loads the models
print('loaded nlgeval..')

# Parameters
data_folder = '../data_outdoor_full_coco'  # folder with data files saved by create_input_files.py
data_name = 'coco_5_cap_per_img_5_min_word_freq'  # base name shared by data files
# checkpoint='checkpoints/full_data_pretained_embedding_300/BEST_checkpoint_coco_5_cap_per_img_5_min_word_freq.pth.tar'
word_map_file = f'{data_folder}/WORDMAP_{data_name}.json'  # word map, ensure it's the same the data was encoded with and the model was trained with
# word_map_file = '/data/code/WORDMAP_pretrained_coco.json'
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # sets device for model and PyTorch tensors
cudnn.benchmark = True  # set to true only if inputs to model are fixed size; otherwise lot of computational overhead


# Load model

def load_model(checkpoint):
    checkpoint = torch.load(checkpoint)
    decoder = checkpoint['decoder']
    decoder = decoder.to(device)
    decoder.eval()
    encoder = checkpoint['encoder']
    encoder = encoder.to(device)
    encoder.eval()
    return encoder, decoder

# Load word map (word2ix)
with open(word_map_file, 'r') as j:
    word_map = json.load(j)
rev_word_map = {v: k for k, v in word_map.items()}
vocab_size = len(word_map)

# Normalization transform
normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])

#Currently testing on (0.000001, 3) (3, 6) and (0.000001, 6)
min_sigma = 1e-10 #0.000001
max_sigma = 6 #3
#blur = transforms.GaussianBlur(5, sigma=(min_sigma, max_sigma))
dataset = 'TEST'
toblur = False
data_folder_blur = '../data_outdoor_full_coco_blurred/min1e-10tomax6'

def evaluate(beam_size, checkpoint):
    encoder, decoder = load_model(checkpoint)
    """
    Evaluation

    :param beam_size: beam size at which to generate captions for evaluation
    :return: BLEU-4 score
    """
    # print('min sigma', min_sigma)
    # print('max sigma', max_sigma)
    if toblur == False:
        # DataLoader
        loader = torch.utils.data.DataLoader(
            CaptionDataset(data_folder, data_name, dataset, transform=transforms.Compose([normalize])),
            batch_size=1, shuffle=False, num_workers=1, pin_memory=True)
    else:
        loader = torch.utils.data.DataLoader(
            CaptionDataset(data_folder_blur, data_name, dataset, transform=transforms.Compose([normalize])),
            batch_size=1, shuffle=False, num_workers=1, pin_memory=True)        

    # TODO: Batched Beam Search
    # Therefore, do not use a batch_size greater than 1 - IMPORTANT!

    # Lists to store references (true captions), and hypothesis (prediction) for each image
    # If for n images, we have n hypotheses, and references a, b, c... for each image, we need -
    # references = [[ref1a, ref1b, ref1c], [ref2a, ref2b], ...], hypotheses = [hyp1, hyp2, ...]
    references = list()
    hypotheses = list()
    
    individual_metric_scores = []
    count = 0
    # For each image
    for i, (image, caps, caplens, allcaps) in enumerate(
            tqdm(loader, desc="EVALUATING AT BEAM SIZE " + str(beam_size))):

        k = beam_size
        # Move to GPU device, if available
        image = image.to(device)  # (1, 3, 256, 256)

        # Encode
        encoder_out = encoder(image)  # (1, enc_image_size, enc_image_size, encoder_dim)
        enc_image_size = encoder_out.size(1)
        encoder_dim = encoder_out.size(3)

        # Flatten encoding
        encoder_out = encoder_out.view(1, -1, encoder_dim)  # (1, num_pixels, encoder_dim)
        num_pixels = encoder_out.size(1)

        # We'll treat the problem as having a batch size of k
        encoder_out = encoder_out.expand(k, num_pixels, encoder_dim)  # (k, num_pixels, encoder_dim)

        # Tensor to store top k previous words at each step; now they're just <start>
        k_prev_words = torch.LongTensor([[word_map['<start>']]] * k).to(device)  # (k, 1)

        # Tensor to store top k sequences; now they're just <start>
        seqs = k_prev_words  # (k, 1)

        # Tensor to store top k sequences' scores; now they're just 0
        top_k_scores = torch.zeros(k, 1).to(device)  # (k, 1)
        seqs_alpha = torch.ones(k, 1, enc_image_size, enc_image_size).to(device)  # (k, 1, enc_image_size, enc_image_size)
        # Lists to store completed sequences and scores
        complete_seqs = list()
        complete_seqs_alpha = list()
        complete_seqs_scores = list()

        # Start decoding
        step = 1
        h, c = decoder.init_hidden_state(encoder_out)

        # s is a number less than or equal to k, because sequences are removed from this process once they hit <end>
        while True:

            embeddings = decoder.embedding(k_prev_words).squeeze(1)  # (s, embed_dim)

            awe, alpha = decoder.attention(encoder_out, h)  # (s, encoder_dim), (s, num_pixels)
            
            alpha = alpha.view(-1, enc_image_size, enc_image_size)
            gate = decoder.sigmoid(decoder.f_beta(h))  # gating scalar, (s, encoder_dim)
            awe = gate * awe

            h, c = decoder.decode_step(torch.cat([embeddings, awe], dim=1), (h, c))  # (s, decoder_dim)

            scores = decoder.fc(h)  # (s, vocab_size)
            scores = F.log_softmax(scores, dim=1)

            # Add
            scores = top_k_scores.expand_as(scores) + scores  # (s, vocab_size)

            # For the first step, all k points will have the same scores (since same k previous words, h, c)
            if step == 1:
                top_k_scores, top_k_words = scores[0].topk(k, 0, True, True)  # (s)
            else:
                # Unroll and find top scores, and their unrolled indices
                top_k_scores, top_k_words = scores.view(-1).topk(k, 0, True, True)  # (s)

            # Convert unrolled indices to actual indices of scores
            prev_word_inds = torch.floor_divide(top_k_words, vocab_size)  # (s)
            next_word_inds = top_k_words % vocab_size  # (s)

            # Add new words to sequences
            seqs = torch.cat([seqs[prev_word_inds], next_word_inds.unsqueeze(1)], dim=1)  # (s, step+1)
            seqs_alpha = torch.cat([seqs_alpha[prev_word_inds], alpha[prev_word_inds].unsqueeze(1)],
                               dim=1)  # (s, step+1, enc_image_size, enc_image_size)

            # Which sequences are incomplete (didn't reach <end>)?
            incomplete_inds = [ind for ind, next_word in enumerate(next_word_inds) if
                               next_word != word_map['<end>']]
            complete_inds = list(set(range(len(next_word_inds))) - set(incomplete_inds))

            # Set aside complete sequences
            if len(complete_inds) > 0:
                complete_seqs.extend(seqs[complete_inds].tolist())
                complete_seqs_alpha.extend(seqs_alpha[complete_inds].tolist())
                complete_seqs_scores.extend(top_k_scores[complete_inds])
            k -= len(complete_inds)  # reduce beam length accordingly

            # Proceed with incomplete sequences
            if k == 0:
                break
            seqs = seqs[incomplete_inds]
            seqs_alpha = seqs_alpha[incomplete_inds]
            h = h[prev_word_inds[incomplete_inds]]
            c = c[prev_word_inds[incomplete_inds]]
            encoder_out = encoder_out[prev_word_inds[incomplete_inds]]
            top_k_scores = top_k_scores[incomplete_inds].unsqueeze(1)
            k_prev_words = next_word_inds[incomplete_inds].unsqueeze(1)

            # Break if things have been going on too long
            if step > 50:
                break
            step += 1
            
        if (len(complete_seqs_scores) == 0):
            print('No complete sequence, omitting')
            continue
            
        i = complete_seqs_scores.index(max(complete_seqs_scores))
        seq = complete_seqs[i]
        alphas = complete_seqs_alpha[i]
        
        # References
        img_caps = allcaps[0].tolist()
        img_captions = list(
            map(lambda c: [w for w in c if w not in {word_map['<start>'], word_map['<end>'], word_map['<pad>']}],
                img_caps))  # remove <start> and pads
        # references.append(img_captions)

        # Hypotheses
        generated_caption = [w for w in seq if w not in {word_map['<start>'], word_map['<end>'], word_map['<pad>']}]
        # hypotheses.append(generated_caption)
        
        generated_caption_text = getSingleSentence(generated_caption)
        reference_captions_text = [getSingleSentence(caption) for caption in img_captions]
        # print(i)
        #print('Reference_text', reference_captions_text)
        #print(f'Generated text: "{generated_caption_text}"')
        scores  = nlgeval.compute_individual_metrics(reference_captions_text, generated_caption_text)
        blue3 = scores['Bleu_3']
        individual_metric_scores.append([count, image, reference_captions_text, generated_caption_text, blue3, seq, alphas])
        
        count = count +1
        if(count>500):
            break
        # assert len(references) == len(hypotheses)
        

    # Calculate BLEU-4 scores
    #references_text, hypotheses_text = getSentences(references, hypotheses)
#     print('references[0]: {}, hypotheses[0]: {} '.format(references_text, hypotheses_text))
    #print(len(references_text), len(hypotheses_text), len(references_text[0]))
    
    # bleu4 = corpus_bleu(references, hypotheses)
    # metrics_dict = nlgeval.compute_metrics(np.array(references_text).T.tolist(), hypotheses_text)
    
    # print(metrics_dict)
    return individual_metric_scores

def getSentences(references, hypotheses):
    references_text = [[getSingleSentence(ref) for ref in refs] for refs in references]
    hypotheses_text = [getSingleSentence(h) for h in hypotheses]
    return references_text, hypotheses_text

def getSingleSentence(s):
    return ' '.join([rev_word_map[w] for w in s])